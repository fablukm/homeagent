from datetime import datetime
from uuid import UUID, uuid7

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID_LEN = 256
NAME_LEN = 64
HASH_LEN = 64
SHA_LEN = 40
KEY_LEN = 32
SHORT_LEN = 16
SIZE_LEN = 4
MODE_LEN = 8


class Base(DeclarativeBase):
    pass


def _uuid() -> UUID:
    return uuid7()


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (UniqueConstraint("source", "external_id", "payload_hash"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(NAME_LEN))
    external_id: Mapped[str] = mapped_column(String(ID_LEN))
    external_version: Mapped[str] = mapped_column(String(ID_LEN), default="")
    occurred_at: Mapped[int] = mapped_column(Integer)
    ingested_at: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    status: Mapped[str] = mapped_column(String(SHORT_LEN), default="ok")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class Occurrence(Base):
    __tablename__ = "occurrences"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    calendar: Mapped[str] = mapped_column(String(NAME_LEN))
    title: Mapped[str] = mapped_column(Text)
    creator: Mapped[str] = mapped_column(String(ID_LEN), default="")
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    tokens: Mapped[str] = mapped_column(String(ID_LEN), default="")
    latest_raw_event_id: Mapped[UUID] = mapped_column(Uuid)
    deleted_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Mail(Base):
    __tablename__ = "mails"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(NAME_LEN))
    sender: Mapped[str] = mapped_column(String(ID_LEN))
    subject: Mapped[str] = mapped_column(Text)
    received_at: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(NAME_LEN), default="")
    latest_raw_event_id: Mapped[UUID] = mapped_column(Uuid)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    list_name: Mapped[str] = mapped_column(String(NAME_LEN))
    title: Mapped[str] = mapped_column(Text)
    starred: Mapped[bool] = mapped_column(default=False)
    due: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size: Mapped[str | None] = mapped_column(String(SIZE_LEN), nullable=True)
    completed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_raw_event_id: Mapped[UUID] = mapped_column(Uuid)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_ts", "ts"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    ts: Mapped[int] = mapped_column(Integer)
    task_class: Mapped[str] = mapped_column(String(KEY_LEN))
    backend: Mapped[str] = mapped_column(String(KEY_LEN))
    model: Mapped[str] = mapped_column(String(ID_LEN))
    prompt_hash: Mapped[str] = mapped_column(String(NAME_LEN))
    schema_name: Mapped[str] = mapped_column(String(NAME_LEN))
    output: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    valid: Mapped[bool] = mapped_column(default=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    load_ms: Mapped[int] = mapped_column(Integer, default=0)
    eval_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class RuleRun(Base):
    __tablename__ = "rule_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    raw_event_id: Mapped[UUID] = mapped_column(ForeignKey("raw_events.id"))
    rule_id: Mapped[str] = mapped_column(String(NAME_LEN))
    rule_sha: Mapped[str] = mapped_column(String(NAME_LEN))
    matched: Mapped[bool]
    llm_call_id: Mapped[UUID | None] = mapped_column(ForeignKey("llm_calls.id"), nullable=True)
    decided_at: Mapped[int] = mapped_column(Integer)


class ActionRow(Base):
    __tablename__ = "actions"
    __table_args__ = (Index("ix_actions_ts", "ts"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    ts: Mapped[int] = mapped_column(Integer)
    rule_run_id: Mapped[UUID] = mapped_column(ForeignKey("rule_runs.id"))
    type: Mapped[str] = mapped_column(String(KEY_LEN))
    params: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(KEY_LEN), unique=True)
    status: Mapped[str] = mapped_column(String(SHORT_LEN))
    external_ref: Mapped[str] = mapped_column(String(ID_LEN), default="")
    executed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str | None] = mapped_column(String(SHORT_LEN), nullable=True)
    outcome_source: Mapped[str | None] = mapped_column(String(SHORT_LEN), nullable=True)
    outcome_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[int] = mapped_column(Integer)
    chat_text: Mapped[str] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(KEY_LEN), default="")
    proposal: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(SHORT_LEN))
    git_sha: Mapped[str] = mapped_column(String(SHA_LEN), default="")
    reverted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Label(Base):
    __tablename__ = "labels"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    question_id: Mapped[str] = mapped_column(String(NAME_LEN))
    raw_event_id: Mapped[UUID] = mapped_column(ForeignKey("raw_events.id"))
    state_text: Mapped[str] = mapped_column(Text)
    options: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    model_answer: Mapped[str] = mapped_column(String(NAME_LEN), default="")
    gold_answer: Mapped[str] = mapped_column(String(NAME_LEN))
    source: Mapped[str] = mapped_column(String(SHORT_LEN))
    created_at: Mapped[int] = mapped_column(Integer)


class Cursor(Base):
    __tablename__ = "cursors"

    source: Mapped[str] = mapped_column(String(NAME_LEN), primary_key=True)
    key: Mapped[str] = mapped_column(String(NAME_LEN), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ModeLog(Base):
    __tablename__ = "mode_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_mode: Mapped[str] = mapped_column(String(MODE_LEN))
    to_mode: Mapped[str] = mapped_column(String(MODE_LEN))
    reason: Mapped[str] = mapped_column(String(KEY_LEN))
    at: Mapped[int] = mapped_column(Integer)


class TaskSize(Base):
    __tablename__ = "task_sizes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(ID_LEN))
    size: Mapped[str] = mapped_column(String(SIZE_LEN))
    source: Mapped[str] = mapped_column(String(MODE_LEN))
    at: Mapped[int] = mapped_column(Integer)


class FintrackWrite(Base):
    __tablename__ = "fintrack_writes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firefly_id: Mapped[str] = mapped_column(String(KEY_LEN))
    source_mail_id: Mapped[str] = mapped_column(String(ID_LEN), default="")
    task_id: Mapped[str] = mapped_column(String(ID_LEN), default="")
    idempotency_key: Mapped[str] = mapped_column(String(KEY_LEN), unique=True)
    at: Mapped[int] = mapped_column(Integer)


def epoch(moment: datetime) -> int:
    return int(moment.timestamp())
