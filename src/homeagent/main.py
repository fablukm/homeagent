import asyncio
import sys
from datetime import datetime
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
from homeagent.core.mode import compute_mode
from homeagent.core.rules import RuleEngine, TomlRuleStore
from homeagent.core.scheduler import Scheduler
from homeagent.db.session import make_engine, upgrade_db
from homeagent.llm.router import LlmRouter
from homeagent.sinks.base import Topic
from homeagent.sinks.telegram import TelegramSink

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
            settings.telegram, secrets.telegram_token.get_secret_value(), self.status
        )

    async def status(self) -> str:
        now = datetime.now(ZoneInfo(self.settings.app.timezone))
        mode = compute_mode(now, self.settings.mode)
        rules = len(self.rules.store.reload())
        uptime = now - self.started_at
        hours, minutes = divmod(uptime.seconds // SECONDS_PER_MINUTE, MINUTES_PER_HOUR)
        return f"up {hours}h{minutes:02d}\nmode {mode.mode} ({mode.reason})\n{rules} rules"

    async def run(self) -> None:
        await asyncio.to_thread(upgrade_db)
        self.scheduler.every(
            self.settings.llm.health_interval_min, self.router.refresh_health, "llm_health"
        )
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
