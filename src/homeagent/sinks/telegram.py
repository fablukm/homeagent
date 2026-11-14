import asyncio
from collections.abc import Awaitable, Callable

from telegram import (
    Bot,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.error import TelegramError

from homeagent.config.settings import Telegram
from homeagent.sinks.base import Button, Card, Sink, Topic, clip, register_sink

CommandFn = Callable[[str], Awaitable[str | None]]
FailFn = Callable[[str, Exception], Awaitable[None]]
TapFn = Callable[[str, str, str, int], Awaitable[Card | None]]
ReplyFn = Callable[[str, str, int], Awaitable[Card | None]]
POLL_TIMEOUT_S = 30
SEPARATOR = ":"
TAP_PARTS = 3
ALREADY_TEXT = "already decided"
COMMAND_PREFIX = "/"
BOT_SUFFIX = "@"
UPDATES_SOURCE = "telegram updates"
UPDATE_SOURCE = "telegram update"


def tap_data(button: Button, key: str) -> str:
    return SEPARATOR.join((button.op, button.arg, key))


def parse_tap(data: str) -> tuple[str, str, str] | None:
    parts = data.split(SEPARATOR, TAP_PARTS - 1)
    if len(parts) != TAP_PARTS or not parts[0] or not parts[2]:
        return None
    op, arg, key = parts
    return op, arg, key


def markup(card: Card) -> InlineKeyboardMarkup | None:
    if not card.buttons:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(b.label, callback_data=tap_data(b, card.key)) for b in row]
            for row in card.buttons
        ]
    )


@register_sink("telegram")
class TelegramSink(Sink):
    name = "telegram"

    def __init__(
        self,
        cfg: Telegram,
        token: str,
        command: CommandFn,
        tap: TapFn,
        reply: ReplyFn,
        failed: FailFn,
    ) -> None:
        self.cfg = cfg
        self.command = command
        self.failed = failed
        self.tap = tap
        self.reply = reply
        self.bot = Bot(token)
        self._offset: int | None = None
        self._task: asyncio.Task[None] | None = None

    def _limit(self, topic: Topic) -> int:
        return self.cfg.limits.log_lines if topic is Topic.LOG else self.cfg.limits.other_lines

    async def emit(self, topic: Topic, text: str) -> None:
        await self.bot.send_message(
            chat_id=self.cfg.chat_id,
            message_thread_id=self.cfg.topics[topic.value],
            text=clip(text, self._limit(topic)),
        )

    async def ask(self, topic: Topic, card: Card) -> str:
        message = await self.bot.send_message(
            chat_id=self.cfg.chat_id,
            message_thread_id=self.cfg.topics[topic.value],
            text=clip(card.text, self._limit(topic)),
            reply_markup=markup(card),
        )
        return str(message.message_id)

    async def start(self) -> None:
        await self.bot.initialize()
        self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self.bot.shutdown()

    async def _poll(self) -> None:
        while True:
            try:
                updates = await self.bot.get_updates(offset=self._offset, timeout=POLL_TIMEOUT_S)
            except TelegramError as exc:
                await self.failed(UPDATES_SOURCE, exc)
                await asyncio.sleep(POLL_TIMEOUT_S)
                continue
            for update in updates:
                self._offset = update.update_id + 1
                await self.on_update(update)

    async def on_update(self, update: Update) -> None:
        try:
            if update.callback_query is not None:
                await self.on_tap(update.callback_query)
            else:
                await self.on_message(update)
        except Exception as exc:
            await self.failed(UPDATE_SOURCE, exc)

    async def on_tap(self, query: CallbackQuery) -> None:
        parsed = parse_tap(query.data or "")
        if parsed is None or query.from_user.id not in self.cfg.allowed_user_ids:
            await query.answer()
            return
        op, arg, key = parsed
        card = await self.tap(key, op, arg, query.from_user.id)
        if card is None:
            await query.answer(ALREADY_TEXT)
            return
        await query.answer()
        text = clip(card.text, self._limit(Topic.APPROVAL))
        if isinstance(query.message, Message) and query.message.text == text:
            return
        await query.edit_message_text(text, reply_markup=markup(card))

    async def on_message(self, update: Update) -> None:
        message = update.message
        if message is None or message.from_user is None or not message.text:
            return
        if message.from_user.id not in self.cfg.allowed_user_ids:
            return
        name = message.text.strip().lower().removeprefix(COMMAND_PREFIX).partition(BOT_SUFFIX)[0]
        answer = await self.command(name)
        if answer is not None:
            await message.reply_text(answer)
            return
        replied = message.reply_to_message
        if replied is None:
            return
        card = await self.reply(str(replied.message_id), message.text, message.from_user.id)
        if card is None:
            return
        text = clip(card.text, self._limit(Topic.APPROVAL))
        if text == replied.text:
            return
        await self.bot.edit_message_text(
            text,
            chat_id=self.cfg.chat_id,
            message_id=replied.message_id,
            reply_markup=markup(card),
        )
