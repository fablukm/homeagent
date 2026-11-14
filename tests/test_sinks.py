from datetime import UTC, datetime
from uuid import uuid7

from telegram import Bot, CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from homeagent.config.settings import Settings
from homeagent.sinks.base import Button, Card, Topic, clip
from homeagent.sinks.stdout import StdoutSink
from homeagent.sinks.telegram import TelegramSink, markup, parse_tap, tap_data

TELEGRAM_CALLBACK_BYTES = 64


def test_clip_keeps_two_lines_and_marks_truncation() -> None:
    assert clip("a\nb\nc", 2) == "a\n…"
    assert clip("a\nb", 2) == "a\nb"


async def test_stdout_sink_records() -> None:
    sink = StdoutSink()
    await sink.emit(Topic.LOG, "hello")
    assert sink.lines == [(Topic.LOG, "hello")]


async def test_stdout_sink_records_questions() -> None:
    sink = StdoutSink()
    card = Card("k1", "pay")
    ref = await sink.ask(Topic.APPROVAL, card)
    assert sink.asked == [(Topic.APPROVAL, card)]
    assert ref == "1"


def test_tap_round_trips_and_fits_telegram() -> None:
    key = str(uuid7())
    longest = Button("appointment", "act", "appointment")
    assert parse_tap(tap_data(longest, key)) == ("act", "appointment", key)
    assert parse_tap(tap_data(Button("Accept", "ok"), key)) == ("ok", "", key)
    assert len(tap_data(longest, key).encode()) <= TELEGRAM_CALLBACK_BYTES


def test_unknown_taps_are_ignored() -> None:
    assert parse_tap("") is None
    assert parse_tap("maybe") is None
    assert parse_tap("ok::") is None
    assert parse_tap(":x:abc") is None


def test_markup_keeps_rows_and_a_decided_card_has_none() -> None:
    rows = ((Button("Accept", "ok"), Button("Nothing to do", "no")), (Button("pay", "act", "pay"),))
    keyboard = markup(Card("k1", "text", rows))

    assert keyboard is not None
    assert [[b.text for b in row] for row in keyboard.inline_keyboard] == [
        ["Accept", "Nothing to do"],
        ["pay"],
    ]
    assert keyboard.inline_keyboard[1][0].callback_data == "act:pay:k1"
    assert markup(Card("k1", "text")) is None


class RecordingBot(Bot):
    answers: list[str | None]
    edits: list[tuple[str, int | None, InlineKeyboardMarkup | None]]

    def __init__(self) -> None:
        super().__init__("1:fake")
        object.__setattr__(self, "answers", [])
        object.__setattr__(self, "edits", [])

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, *args: object, **kwargs: object
    ) -> bool:
        self.answers.append(text)
        return True

    async def edit_message_text(
        self,
        text: str,
        chat_id: str | int | None = None,
        message_id: int | None = None,
        *args: object,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kwargs: object,
    ) -> bool:
        self.edits.append((text, message_id, reply_markup))
        return True


SHOWN = "pay\nPay the bill\nMail from Billing"
CHANGED = Card("k1", "todo\nPay the bill\nMail from Billing", ((Button("pay", "act", "pay"),),))
OWNER = 7


class Failures:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    async def __call__(self, where: str, exc: Exception) -> None:
        self.seen.append((where, str(exc)))


def _sink(
    settings: Settings, card: Card | None, failures: Failures | None = None
) -> tuple[TelegramSink, RecordingBot]:
    async def command(name: str) -> str | None:
        return f"did {name}" if name in {"status", "stop", "start"} else None

    async def tap(key: str, op: str, arg: str, user_id: int) -> Card | None:
        return card

    async def reply(ref: str, text: str, user_id: int) -> Card | None:
        return card if ref == "5" else None

    cfg = settings.telegram.model_copy(update={"allowed_user_ids": [OWNER]})
    sink = TelegramSink(cfg, "1:fake", command, tap, reply, failures or Failures())
    bot = RecordingBot()
    sink.bot = bot
    return sink, bot


def _shown(bot: Bot) -> Message:
    chat = Chat(1, Chat.SUPERGROUP)
    message = Message(5, datetime(2026, 11, 14, tzinfo=UTC), chat, text=SHOWN)
    message.set_bot(bot)
    return message


def _query(bot: Bot, user: int, data: str) -> CallbackQuery:
    query = CallbackQuery("q1", User(user, "u", False), "c", message=_shown(bot), data=data)
    query.set_bot(bot)
    return query


async def test_a_tap_redraws_the_card_only_when_it_changed(settings: Settings) -> None:
    sink, bot = _sink(settings, CHANGED)
    await sink.on_tap(_query(bot, OWNER, "act:todo:k1"))
    [(text, message_id, keyboard)] = bot.edits
    assert (text, message_id) == (CHANGED.text, 5)
    assert keyboard == markup(CHANGED)

    same, same_bot = _sink(settings, Card("k1", SHOWN))
    await same.on_tap(_query(same_bot, OWNER, "due:today:k1"))
    assert same_bot.edits == []
    assert same_bot.answers == [None]


async def test_taps_from_strangers_or_on_decided_cards_change_nothing(settings: Settings) -> None:
    sink, bot = _sink(settings, CHANGED)
    await sink.on_tap(_query(bot, OWNER + 1, "ok::k1"))
    await sink.on_tap(_query(bot, OWNER, "garbage"))
    assert bot.edits == []
    assert bot.answers == [None, None]

    decided, decided_bot = _sink(settings, None)
    await decided.on_tap(_query(decided_bot, OWNER, "ok::k1"))
    assert decided_bot.answers == ["already decided"]
    assert decided_bot.edits == []


async def test_a_reply_to_a_card_sets_its_text(settings: Settings) -> None:
    sink, bot = _sink(settings, CHANGED)
    chat = Chat(1, Chat.SUPERGROUP)
    user = User(OWNER, "u", False)
    when = datetime(2026, 11, 14, tzinfo=UTC)
    answer = Message(
        6, when, chat, from_user=user, text="Pay the bill", reply_to_message=_shown(bot)
    )
    stray = Message(7, when, chat, from_user=user, text="hello")
    await sink.on_message(Update(1, message=answer))
    await sink.on_message(Update(2, message=stray))
    [(text, message_id, keyboard)] = bot.edits
    assert (text, message_id, keyboard) == (CHANGED.text, 5, markup(CHANGED))


class ReplyingBot(RecordingBot):
    sent: list[str]

    def __init__(self) -> None:
        super().__init__()
        object.__setattr__(self, "sent", [])

    async def send_message(
        self, chat_id: int | str, text: str, *args: object, **kwargs: object
    ) -> Message:
        self.sent.append(text)
        return _shown(self)


def _message(bot: Bot, text: str, user: int = OWNER) -> Update:
    chat = Chat(1, Chat.SUPERGROUP)
    when = datetime(2026, 11, 14, tzinfo=UTC)
    message = Message(9, when, chat, from_user=User(user, "u", False), text=text)
    message.set_bot(bot)
    return Update(3, message=message)


async def test_commands_answer_with_or_without_slash_and_bot_name(settings: Settings) -> None:
    sink, _ = _sink(settings, CHANGED)
    bot = ReplyingBot()
    sink.bot = bot

    for text in ("status", "/Stop@homeagent_bot", " START ", "hello"):
        await sink.on_message(_message(bot, text))
    await sink.on_message(_message(bot, "status", OWNER + 1))

    assert bot.sent == ["did status", "did stop", "did start"]
    assert bot.edits == []


async def test_a_failing_update_is_reported_and_polling_goes_on(settings: Settings) -> None:
    failures = Failures()

    async def broken(key: str, op: str, arg: str, user_id: int) -> Card | None:
        raise RuntimeError("database locked")

    sink, bot = _sink(settings, CHANGED, failures)
    sink.tap = broken

    await sink.on_update(Update(4, callback_query=_query(bot, OWNER, "ok::k1")))

    assert failures.seen == [("telegram update", "database locked")]
