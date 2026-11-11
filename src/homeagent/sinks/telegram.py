import asyncio
from collections.abc import Awaitable, Callable

from telegram import Bot, Update

from homeagent.config.settings import Telegram
from homeagent.sinks.base import Sink, Topic, clip, register_sink

StatusFn = Callable[[], Awaitable[str]]
POLL_TIMEOUT_S = 30


@register_sink("telegram")
class TelegramSink(Sink):
    name = "telegram"

    def __init__(self, cfg: Telegram, token: str, status: StatusFn) -> None:
        self.cfg = cfg
        self.status = status
        self.bot = Bot(token)
        self._offset: int | None = None
        self._task: asyncio.Task[None] | None = None

    async def emit(self, topic: Topic, text: str) -> None:
        limit = self.cfg.limits.log_lines if topic is Topic.LOG else self.cfg.limits.other_lines
        await self.bot.send_message(
            chat_id=self.cfg.chat_id,
            message_thread_id=self.cfg.topics[topic.value],
            text=clip(text, limit),
        )

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
                await self._handle(update)

    async def _handle(self, update: Update) -> None:
        message = update.message
        if message is None or message.from_user is None or not message.text:
            return
        if message.from_user.id not in self.cfg.allowed_user_ids:
            return
        if message.text.strip().lower() in {"status", "/status"}:
            await message.reply_text(await self.status())
