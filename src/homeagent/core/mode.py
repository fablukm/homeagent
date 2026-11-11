from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from homeagent.config.settings import Mode as ModeCfg


class Mode(StrEnum):
    WORK = "work"
    PRIVATE = "private"
    OFF = "off"


@dataclass(frozen=True)
class ModeDecision:
    mode: Mode
    reason: str


@dataclass(frozen=True)
class ManualOverride:
    mode: Mode
    until: datetime


def _in_window(now: time, window: str) -> bool:
    start_s, end_s = window.split("-")
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def compute_mode(
    now: datetime,
    cfg: ModeCfg,
    *,
    manual: ManualOverride | None = None,
    holiday: bool = False,
    work_event_within: timedelta | None = None,
    family_event_running: bool = False,
) -> ModeDecision:
    if manual is not None and now < manual.until:
        return ModeDecision(manual.mode, "manual")
    if _in_window(now.time(), cfg.overrides.off_window):
        return ModeDecision(Mode.OFF, "off_window")
    if holiday:
        return ModeDecision(Mode.PRIVATE, "holiday")
    lookahead = timedelta(minutes=cfg.overrides.work_event_lookahead_min)
    if work_event_within is not None and work_event_within <= lookahead:
        return ModeDecision(Mode.WORK, "work_event")
    if family_event_running and cfg.overrides.family_event_in_work:
        return ModeDecision(Mode.PRIVATE, "family_event")
    windows = cfg.hours.get(now.strftime("%a").lower(), [])
    if any(_in_window(now.time(), w) for w in windows):
        return ModeDecision(Mode.WORK, "hours")
    return ModeDecision(Mode.PRIVATE, "hours")
