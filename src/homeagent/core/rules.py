import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from homeagent.config.settings import Schema
from homeagent.core.events import Event


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    calendars: list[str] = Field(default_factory=list)


class Match(BaseModel):
    model_config = ConfigDict(extra="forbid")

    creator: str | None = None
    sender: str | None = None
    label: str | None = None
    title_regex: str | None = None
    title_llm: str | None = None
    tokens_any: list[str] = Field(default_factory=list)
    mode: str | None = None
    weekday: list[str] = Field(default_factory=list)
    kind: str | None = None


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    order: str | None = None
    trigger: Trigger
    match: Match = Field(default_factory=Match)
    actions: list[ActionSpec]


class RuleValidationError(ValueError):
    pass


def validate_against_schema(rule: Rule, schema: Schema) -> None:
    if rule.trigger.source not in schema.trigger_sources:
        raise RuleValidationError(
            f"{rule.id}: trigger.source {rule.trigger.source!r} not in schema"
        )
    for spec in rule.actions:
        if spec.type not in schema.action_types:
            raise RuleValidationError(f"{rule.id}: action type {spec.type!r} not in schema")
    for key, value in rule.match.model_dump(exclude_defaults=True).items():
        if value and key not in schema.match_keys:
            raise RuleValidationError(f"{rule.id}: match key {key!r} not in schema")


def load_rule(path: Path, schema: Schema) -> Rule:
    with path.open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    rule = Rule.model_validate(data)
    validate_against_schema(rule, schema)
    return rule


class TomlRuleStore:
    def __init__(self, rules_dir: Path, schema: Schema) -> None:
        self.rules_dir = rules_dir
        self.schema = schema
        self._rules: dict[str, Rule] = {}
        self._mtimes: dict[Path, float] = {}

    def reload(self) -> list[Rule]:
        current = {p: p.stat().st_mtime for p in sorted(self.rules_dir.glob("*.toml"))}
        if current != self._mtimes:
            self._rules = {r.id: r for r in (load_rule(p, self.schema) for p in current)}
            self._mtimes = current
        return list(self._rules.values())


class RuleEngine:
    def __init__(self, store: TomlRuleStore) -> None:
        self.store = store

    def matching(self, event: Event, mode: str) -> list[Rule]:
        return [r for r in self.store.reload() if self.matches(r, event, mode)]

    @staticmethod
    def matches(rule: Rule, event: Event, mode: str) -> bool:
        trigger, match = rule.trigger, rule.match
        if trigger.source != event.source.value:
            return False
        if trigger.calendars and event.calendar not in trigger.calendars:
            return False
        checks = (
            match.creator is None or match.creator == event.creator,
            match.sender is None or match.sender == event.sender,
            match.mode is None or match.mode == mode,
            not match.tokens_any or bool(event.tokens & set(match.tokens_any)),
            match.title_regex is None or re.search(match.title_regex, event.title) is not None,
            not match.weekday or event.occurred_at.strftime("%a").lower() in match.weekday,
        )
        return all(checks)
