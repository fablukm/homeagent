import asyncio
import logging
import sys
import time
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

import structlog
import uvicorn
from pydantic import SecretStr

import homeagent.actions.telegram_log
import homeagent.llm.anthropic
import homeagent.llm.laya
import homeagent.llm.ollama
import homeagent.llm.openai_compat
import homeagent.sinks.stdout
import homeagent.sinks.telegram
from homeagent.api.app import build_app
from homeagent.config.settings import App, Secrets, Settings, load_settings
from homeagent.core.dispatch import Dispatcher
from homeagent.core.events import Event, SourceKind
from homeagent.core.ingest import Ingestor
from homeagent.core.mode import compute_mode
from homeagent.core.rules import RuleEngine, TomlRuleStore
from homeagent.core.scheduler import Scheduler
from homeagent.core.triage import Triage
from homeagent.db.models import Cursor
from homeagent.db.session import make_engine, make_sessions, upgrade_db
from homeagent.llm.base import DECISION_BACKENDS
from homeagent.llm.router import LlmRouter
from homeagent.sinks.base import Card, Sink, Topic
from homeagent.sinks.telegram import TelegramSink
from homeagent.sources.base import Source
from homeagent.sources.gmail import GmailSource

GMAIL = "gmail"
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
AGENT_STATE = ("agent", "paused")
PAUSED = "1"
RUNNING = "0"
STOP = "stop"
START = "start"
STATUS = "status"
STOPPED_TEXT = "stopped: mail is not read until start"
STARTED_TEXT = "started: mail is read again"
MAIL_STOPPED = "mail stopped"
MAIL_RUNNING = "mail running"
REDACTED = "<secret>"
_ = (
    homeagent.actions.telegram_log,
    homeagent.llm.anthropic,
    homeagent.llm.laya,
    homeagent.llm.ollama,
    homeagent.llm.openai_compat,
    homeagent.sinks.stdout,
    homeagent.sinks.telegram,
)


class Agent:
    def __init__(self, settings: Settings, secrets: Secrets) -> None:
        self.settings = settings
        self.secrets = secrets
        self.started_at = datetime.now(ZoneInfo(settings.app.timezone))
        self.log = structlog.get_logger("agent")
        self.engine = make_engine(settings.app.data_dir / settings.app.db_file)
        self.rules = RuleEngine(TomlRuleStore(settings.app.rules_dir, settings.schema_))
        self.router = LlmRouter(settings.llm, secrets)
        self.scheduler = Scheduler(settings.app.timezone)
        self.sink: Sink = TelegramSink(
            settings.telegram,
            secrets.telegram_token.get_secret_value(),
            self.command,
            self.tap,
            self.reply,
            self.report,
        )
        self.paused = False
        self.reported: dict[str, tuple[str, float]] = {}
        self.secret_values = [
            value.get_secret_value()
            for value in secrets.model_dump().values()
            if isinstance(value, SecretStr) and value.get_secret_value()
        ]
        sessions = make_sessions(self.engine)
        self.sessions = sessions
        self.ingestor = Ingestor(sessions)
        self.dispatcher = Dispatcher(sessions, self.rules, self.sink)
        decider_name = settings.triage.laya.backend
        decider_cfg = settings.llm.backends[decider_name]
        decider = DECISION_BACKENDS.get(decider_cfg.type)(decider_name, decider_cfg)
        self.triage = Triage(
            settings.triage,
            settings.preferences,
            sessions,
            self.router,
            decider,
            self.sink,
            settings.app.timezone,
        )
        self.sources: list[tuple[Source, int]] = [
            (GmailSource(cfg, settings.sources.google, secrets), cfg.interval_min)
            for cfg in settings.sources.email
            if cfg.name == GMAIL and secrets.google_refresh_token
        ]

    async def status(self) -> str:
        now = datetime.now(ZoneInfo(self.settings.app.timezone))
        mode = compute_mode(now, self.settings.mode)
        rules = len(self.rules.store.reload())
        uptime = now - self.started_at
        hours, minutes = divmod(uptime.seconds // SECONDS_PER_MINUTE, MINUTES_PER_HOUR)
        lines = [
            f"up {hours}h{minutes:02d}",
            self.mail_state(),
            f"mode {mode.mode} ({mode.reason})",
            f"{rules} rules",
        ]
        return "\n".join(lines)

    def mail_state(self) -> str:
        return MAIL_STOPPED if self.paused else MAIL_RUNNING

    async def command(self, name: str) -> str | None:
        if name == STATUS:
            return await self.status()
        if name not in {STOP, START}:
            return None
        self.paused = name == STOP
        source, key = AGENT_STATE
        async with self.sessions() as session, session.begin():
            await session.merge(
                Cursor(source=source, key=key, value=PAUSED if self.paused else RUNNING)
            )
        self.log.info(self.mail_state())
        return STOPPED_TEXT if self.paused else STARTED_TEXT

    async def load_paused(self) -> None:
        async with self.sessions() as session:
            stored = await session.get(Cursor, AGENT_STATE)
        self.paused = stored is not None and stored.value == PAUSED

    async def report(self, where: str, exc: Exception) -> None:
        self.log.error("failed", where=where, exc_info=exc)
        text = f"error in {where}\n{type(exc).__name__}: {exc}"
        for secret in self.secret_values:
            text = text.replace(secret, REDACTED)
        now = time.monotonic()
        last = self.reported.get(where)
        window = self.settings.app.error_repeat_min * SECONDS_PER_MINUTE
        if last is not None and last[0] == text and now - last[1] < window:
            return
        self.reported[where] = (text, now)
        try:
            await self.sink.emit(Topic.LOG, text)
        except Exception:
            self.log.exception("error report not sent", where=where)

    async def tap(self, key: str, op: str, arg: str, user_id: int) -> Card | None:
        return await self.triage.tap(key, op, arg, user_id)

    async def reply(self, ref: str, text: str, user_id: int) -> Card | None:
        return await self.triage.reply(ref, text, user_id)

    async def poll(self, source: Source) -> None:
        if self.paused:
            return
        try:
            report = await self.ingestor.run(source)
        except Exception as exc:
            await self.report(f"poll {source.name}", exc)
            return
        now = datetime.now(ZoneInfo(self.settings.app.timezone))
        mode = compute_mode(now, self.settings.mode).mode
        asked = 0
        for event in report.events:
            asked += await self.handle(event, mode)
        log = self.log.info if report.events or report.quarantined else self.log.debug
        log(
            "polled",
            source=source.name,
            new=len(report.events),
            duplicates=report.duplicates,
            quarantined=report.quarantined,
            asked=asked,
        )

    async def handle(self, event: Event, mode: str) -> int:
        try:
            await self.dispatcher.run(event, mode)
            if event.source is not SourceKind.MAIL:
                return 0
            return int(await self.triage.run(event) is not None)
        except Exception as exc:
            await self.report(f"{event.source_name} {event.external_id}", exc)
            return 0

    async def run(self) -> None:
        await asyncio.to_thread(upgrade_db)
        await self.load_paused()
        self.scheduler.every(
            self.settings.llm.health_interval_min, self.router.refresh_health, "llm_health"
        )
        for source, interval_min in self.sources:
            self.scheduler.every(interval_min, partial(self.poll, source), f"poll_{source.name}")
        self.scheduler.start()
        await self.sink.start()
        now = datetime.now(ZoneInfo(self.settings.app.timezone))
        mode = compute_mode(now, self.settings.mode)
        rules = len(self.rules.store.reload())
        await self.sink.emit(
            Topic.LOG,
            f"started with {rules} rules, {self.mail_state()}\nmode {mode.mode} ({mode.reason})",
        )
        config = uvicorn.Config(
            build_app(self.status),
            host=self.settings.app.api_host,
            port=self.settings.app.api_port,
            log_level=self.settings.app.log_level.lower(),
        )
        try:
            await uvicorn.Server(config).serve()
        finally:
            self.scheduler.stop()
            await self.sink.stop()


def configure_logging(app: App) -> None:
    level = logging.getLevelNamesMapping()[app.log_level]
    logging.basicConfig(level=level, format="%(message)s", force=True)
    for name in app.quiet_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(level))


def cli() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    settings = load_settings()
    if command == "check":
        rules = TomlRuleStore(settings.app.rules_dir, settings.schema_).reload()
        print(f"config ok, {len(rules)} rules")
        return
    if command == "run":
        configure_logging(settings.app)
        secrets = Secrets()
        if not secrets.telegram_token.get_secret_value():
            raise SystemExit("HOMEAGENT_TELEGRAM_TOKEN is not set")
        asyncio.run(Agent(settings, secrets).run())
        return
    raise SystemExit(f"unknown command {command!r}; use run | check")


if __name__ == "__main__":
    cli()
