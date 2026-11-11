from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_DIR = Path("config")
CONFIG_FILES = (
    "app.toml",
    "telegram.toml",
    "llm.toml",
    "sources.toml",
    "schema.toml",
    "mode.toml",
    "load.toml",
    "retention.toml",
    "feedback.toml",
)


class App(BaseModel):
    name: str
    timezone: str
    data_dir: Path
    rules_dir: Path
    db_file: str
    api_host: str
    api_port: int
    log_level: str


class TelegramLimits(BaseModel):
    log_lines: int
    other_lines: int
    batch_minutes: int
    confirm_ttl_hours: int


class Telegram(BaseModel):
    chat_id: int
    topics: dict[Literal["orders", "facts", "log"], int]
    allowed_user_ids: list[int]
    limits: TelegramLimits


type LlmProvider = Literal["openai", "deepseek", "moonshot", "alibaba", "anthropic"]


class LlmBackendCfg(BaseModel):
    type: Literal["ollama", "openai", "anthropic", "laya"]
    url: str = ""
    model: str
    timeout_s: int
    cloud: bool = False
    secret: LlmProvider | None = None
    json_mode: Literal["schema", "object"] = "schema"
    sends_temperature: bool = True


class TaskClassRoute(BaseModel):
    backends: list[str]
    allow_cloud: bool = False
    temperature: float = 0.0


class Llm(BaseModel):
    backends: dict[str, LlmBackendCfg]
    classes: dict[str, TaskClassRoute]
    health_interval_min: int


class EmailSourceCfg(BaseModel):
    name: str
    account: str
    interval_min: int
    labels: list[str]
    backfill_days: int
    timeout_s: int
    url: str = ""


class CalendarSourceCfg(BaseModel):
    name: str
    calendar_id: str
    interval_min: int


class GoogleCfg(BaseModel):
    auth_url: str
    token_url: str
    calendar_url: str
    tasks_url: str
    timeout_s: int
    scopes: list[str]


class Sources(BaseModel):
    google: GoogleCfg
    email: list[EmailSourceCfg] = Field(default_factory=list[EmailSourceCfg])
    calendar: list[CalendarSourceCfg] = Field(default_factory=list[CalendarSourceCfg])
    partner_emails: list[str] = Field(default_factory=list[str])


class Schema(BaseModel):
    trigger_sources: list[str]
    match_keys: list[str]
    action_types: list[str]
    event_tags: list[str]
    task_sizes: list[str]


class ModeOverrides(BaseModel):
    manual_ttl_h: int
    holiday_token: str
    work_event_lookahead_min: int
    family_event_in_work: bool
    off_window: str


class Mode(BaseModel):
    hours: dict[str, list[str]]
    overrides: ModeOverrides


class Load(BaseModel):
    hours_per_size: dict[str, float]
    bands: dict[str, float]
    workshop_day_hours: float
    report_cron: str


class Retention(BaseModel):
    raw_payload_days: int
    llm_calls_days: int
    data_budget_gb: float
    nightly_cron: str


class Feedback(BaseModel):
    accept_window_days: int
    ask_ttl_hours: int


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOMEAGENT_", env_file=".env", extra="ignore")

    telegram_token: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    moonshot_api_key: SecretStr | None = None
    alibaba_api_key: SecretStr | None = None
    google_client_id: SecretStr | None = None
    google_client_secret: SecretStr | None = None
    google_refresh_token: SecretStr | None = None
    paperless_token: SecretStr | None = None
    firefly_token: SecretStr | None = None

    def llm_key(self, provider: LlmProvider | None) -> str:
        keys = {
            "openai": self.openai_api_key,
            "deepseek": self.deepseek_api_key,
            "moonshot": self.moonshot_api_key,
            "alibaba": self.alibaba_api_key,
            "anthropic": self.anthropic_api_key,
        }
        secret = keys[provider] if provider else None
        return secret.get_secret_value() if secret else ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file=[str(CONFIG_DIR / name) for name in CONFIG_FILES], extra="forbid"
    )

    app: App
    telegram: Telegram
    llm: Llm
    sources: Sources
    schema_: Schema = Field(alias="schema")
    mode: Mode
    load: Load
    retention: Retention
    feedback: Feedback

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, TomlConfigSettingsSource(settings_cls))


def load_settings(config_dir: Path = CONFIG_DIR) -> Settings:
    files = [str(config_dir / name) for name in CONFIG_FILES]
    return Settings.model_validate(TomlConfigSettingsSource(Settings, toml_file=files)())
