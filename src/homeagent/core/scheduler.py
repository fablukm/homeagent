from collections.abc import Callable, Coroutine
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

Job = Callable[[], Coroutine[Any, Any, None]]


class Scheduler:
    def __init__(self, timezone: str) -> None:
        self._inner = AsyncIOScheduler(timezone=timezone)

    def every(self, minutes: int, job: Job, name: str) -> None:
        self._inner.add_job(job, IntervalTrigger(minutes=minutes), id=name, replace_existing=True)

    def cron(self, expression: str, job: Job, name: str) -> None:
        trigger = CronTrigger.from_crontab(expression)
        self._inner.add_job(job, trigger, id=name, replace_existing=True)

    def start(self) -> None:
        self._inner.start()

    def stop(self) -> None:
        self._inner.shutdown(wait=False)
