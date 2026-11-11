from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid7

from pydantic import BaseModel, ConfigDict, Field


class SourceKind(StrEnum):
    CALENDAR = "calendar"
    MAIL = "mail"
    TASK = "task"
    TASK_COMPLETED = "task.completed"
    SCHEDULE = "schedule"
    LOCATION = "location"
    GLASSES = "glasses"


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid7)
    source: SourceKind
    source_name: str
    external_id: str
    occurred_at: datetime
    title: str = ""
    body: str = ""
    creator: str = ""
    calendar: str = ""
    sender: str = ""
    tokens: frozenset[str] = frozenset()
    payload: dict[str, Any] = Field(default_factory=dict)
