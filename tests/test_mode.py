from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from homeagent.config.settings import Settings
from homeagent.core.mode import ManualOverride, Mode, compute_mode

ZH = ZoneInfo("Europe/Zurich")


def test_hours_and_off_window(settings: Settings) -> None:
    monday_ten = datetime(2026, 11, 9, 10, tzinfo=ZH)
    assert compute_mode(monday_ten, settings.mode).mode is Mode.WORK
    assert compute_mode(monday_ten.replace(hour=19), settings.mode).mode is Mode.PRIVATE
    assert compute_mode(monday_ten.replace(hour=23), settings.mode).mode is Mode.OFF
    assert compute_mode(monday_ten.replace(day=14), settings.mode).mode is Mode.PRIVATE


def test_overrides_in_precedence(settings: Settings) -> None:
    monday_ten = datetime(2026, 11, 9, 10, tzinfo=ZH)
    family = compute_mode(monday_ten, settings.mode, family_event_running=True)
    assert (family.mode, family.reason) == (Mode.PRIVATE, "family_event")
    evening = monday_ten.replace(hour=19)
    meeting = compute_mode(evening, settings.mode, work_event_within=timedelta(minutes=10))
    assert meeting.mode is Mode.WORK
    manual = ManualOverride(Mode.WORK, evening + timedelta(hours=1))
    assert compute_mode(evening, settings.mode, manual=manual, holiday=True).reason == "manual"
    assert compute_mode(monday_ten, settings.mode, holiday=True).reason == "holiday"
