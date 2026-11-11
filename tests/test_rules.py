from datetime import UTC, datetime
from pathlib import Path

from homeagent.config.settings import Settings
from homeagent.core.events import Event, SourceKind
from homeagent.core.rules import RuleEngine, TomlRuleStore
from homeagent.core.syntax import parse_title

ROOT = Path(__file__).resolve().parent.parent


def _event(title: str, creator: str, calendar: str) -> Event:
    parsed = parse_title(title)
    return Event(
        source=SourceKind.CALENDAR,
        source_name=calendar,
        external_id="evt-1",
        occurred_at=datetime(2026, 11, 20, 9, tzinfo=UTC),
        title=parsed.text,
        creator=creator,
        calendar=calendar,
        tokens=parsed.tokens,
    )


def test_paediatrician_rule_matches_partners_blocker(settings: Settings) -> None:
    engine = RuleEngine(TomlRuleStore(ROOT / "rules.example", settings.schema_))
    partner = settings.sources.partner_emails[0]
    matched = engine.matching(_event("! Kinderarzt #kids", partner, "private"), "work")
    assert [r.id for r in matched] == ["paediatrician"]
    assert [a.type for a in matched[0].actions] == ["calendar.block", "task.create", "telegram.log"]


def test_own_event_without_tokens_matches_nothing(settings: Settings) -> None:
    engine = RuleEngine(TomlRuleStore(ROOT / "rules.example", settings.schema_))
    assert engine.matching(_event("Lunch", "me", "private"), "work") == []


def test_trip_rule_matches_any_calendar(settings: Settings) -> None:
    engine = RuleEngine(TomlRuleStore(ROOT / "rules.example", settings.schema_))
    matched = engine.matching(_event("Berlin workshop #trip", "me", "work"), "work")
    assert [r.id for r in matched] == ["long-trip-charge-car"]
