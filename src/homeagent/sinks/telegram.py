import asyncio
from collections.abc import Awaitable, Callable

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

from homeagent.config.settings import Telegram
from homeagent.sinks.base import Sink, Topic, clip, register_sink

StatusFn = Callable[[], Awaitable[str]]
DecideFn = Callable[[str, bool, int], Awaitable[bool]]
POLL_TIMEOUT_S = 30
ACCEPT = "y"
DECLINE = "n"
SEPARATOR = ":"
ACCEPT_TEXT = "Accept"
DECLINE_TEXT = "Decline"
ACCEPTED_TEXT = "accepted"
DECLINED_TEXT = "declined"
ALREADY_TEXT = "already decided"


def verdict_data(accepted: bool, key: str) -> str:
    return f"{ACCEPT if accepted else DECLINE}{SEPARATOR}{key}"


def parse_verdict(data: str) -> tuple[bool, str] | None:
    verdict, _, key = data.partition(SEPARATOR)
    if verdict not in {ACCEPT, DECLINE} or not key:
        return None
    return verdict == ACCEPT, key


def decided_text(text: str, accepted: bool, max_lines: int) -> str:
    return clip(text, max_lines - 1) + "\n" + (ACCEPTED_TEXT if accepted else DECLINED_TEXT)


@register_sink("telegram")
class TelegramSink(Sink):
    name = "telegram"

    def __init__(self, cfg: Telegram, token: str, status: StatusFn, decide: DecideFn) -> None:
        self.cfg = cfg
        self.status = status
        self.decide = decide
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

    async def ask(self, topic: Topic, text: str, key: str) -> str:
        buttons = [
            InlineKeyboardButton(ACCEPT_TEXT, callback_data=verdict_data(True, key)),
            InlineKeyboardButton(DECLINE_TEXT, callback_data=verdict_data(False, key)),
        ]
        message = await self.bot.send_message(
            chat_id=self.cfg.chat_id,
            message_thread_id=self.cfg.topics[topic.value],
            text=clip(text, self._limit(topic) - 1),
            reply_markup=InlineKeyboardMarkup([buttons]),
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
            updates = await self.bot.get_updates(offset=self._offset, timeout=POLL_TIMEOUT_S)
            for update in updates:
                self._offset = update.update_id + 1
                if update.callback_query is not None:
                    await self._verdict(update.callback_query)
                else:
                    await self._handle(update)

    async def _verdict(self, query: CallbackQuery) -> None:
        verdict = parse_verdict(query.data or "")
        if verdict is None or query.from_user.id not in self.cfg.allowed_user_ids:
            await query.answer()
            return
        accepted, key = verdict
        done = await self.decide(key, accepted, query.from_user.id)
        if not done:
            await query.answer(ALREADY_TEXT)
            return
        await query.answer(ACCEPTED_TEXT if accepted else DECLINED_TEXT)
        if isinstance(query.message, Message) and query.message.text:
            limit = self._limit(Topic.APPROVAL)
            await query.edit_message_text(decided_text(query.message.text, accepted, limit))

    async def _handle(self, update: Update) -> None:
        message = update.message
        if message is None or message.from_user is None or not message.text:
            return
        if message.from_user.id not in self.cfg.allowed_user_ids:
            return
        if message.text.strip().lower() in {"status", "/status"}:
            await message.reply_text(await self.status())
