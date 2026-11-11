import asyncio
import sys
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

import structlog
import uvicorn

import homeagent.actions.telegram_log
import homeagent.llm.anthropic
import homeagent.llm.ollama
import homeagent.llm.openai_compat
import homeagent.sinks.stdout
import homeagent.sinks.telegram
from homeagent.api.app import build_app
from homeagent.config.settings import Secrets, Settings, load_settings
from homeagent.core.dispatch import Dispatcher
from homeagent.core.events import Event, SourceKind
from homeagent.core.ingest import Ingestor
from homeagent.core.mode import compute_mode
from homeagent.core.rules import RuleEngine, TomlRuleStore
from homeagent.core.scheduler import Scheduler
from homeagent.core.triage import Triage
from homeagent.db.session import make_engine, make_sessions, upgrade_db
from homeagent.llm.router import LlmRouter, NoBackendError
from homeagent.sinks.base import Topic
from homeagent.sinks.telegram import TelegramSink
from homeagent.sources.base import Source
from homeagent.sources.gmail import GmailSource

GMAIL = "gmail"
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
_ = (
    homeagent.actions.telegram_log,
    homeagent.llm.anthropic,
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
        self.sink = TelegramSink(
            settings.telegram, secrets.telegram_token.get_secret_value(), self.status, self.decide
        )
        sessions = make_sessions(self.engine)
        self.ingestor = Ingestor(sessions)
        self.dispatcher = Dispatcher(sessions, self.rules, self.sink)
        self.triage = Triage(
            settings.triage, sessions, self.router, self.sink, settings.app.timezone
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
        return f"up {hours}h{minutes:02d}\nmode {mode.mode} ({mode.reason})\n{rules} rules"

    async def decide(self, key: str, accepted: bool, user_id: int) -> bool:
        return await self.triage.decide(key, accepted, user_id)

    async def poll(self, source: Source) -> None:
        report = await self.ingestor.run(source)
        now = datetime.now(ZoneInfo(self.settings.app.timezone))
        mode = compute_mode(now, self.settings.mode).mode
        proposed = 0
        for event in report.events:
            await self.dispatcher.run(event, mode)
            if event.source is SourceKind.MAIL:
                proposed += await self._triage(event)
        self.log.info(
            "polled",
            source=source.name,
            new=len(report.events),
            duplicates=report.duplicates,
            quarantined=report.quarantined,
            proposed=proposed,
        )

    async def _triage(self, event: Event) -> int:
        try:
            return int(await self.triage.run(event) is not None)
        except NoBackendError:
            self.log.exception("triage failed", raw_event_id=str(event.id))
            return 0

    async def run(self) -> None:
        await asyncio.to_thread(upgrade_db)
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
            Topic.LOG, f"started with {rules} rules\nmode {mode.mode} ({mode.reason})"
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


def cli() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    settings = load_settings()
    if command == "check":
        rules = TomlRuleStore(settings.app.rules_dir, settings.schema_).reload()
        print(f"config ok, {len(rules)} rules")
        return
    if command == "run":
        secrets = Secrets()
        if not secrets.telegram_token.get_secret_value():
            raise SystemExit("HOMEAGENT_TELEGRAM_TOKEN is not set")
        asyncio.run(Agent(settings, secrets).run())
        return
    raise SystemExit(f"unknown command {command!r}; use run | check")


if __name__ == "__main__":
    cli()
