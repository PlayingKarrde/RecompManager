"""Periodic update checks (APScheduler). The interval comes from settings."""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from recompmanager import app_settings
from recompmanager.library import CHECK
from recompmanager.models import Recomp, RecompStatus, utcnow

log = logging.getLogger(__name__)
JOB_ID = "check-all"
FIRST_RUN_DELAY = timedelta(minutes=5)


class Scheduler:
    def __init__(self, services) -> None:
        self.services = services
        self._sched = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        with self.services.sessionmaker() as s:
            hours = app_settings.load(s).update_check_interval_hours
        self._sched.add_job(
            self.check_all,
            "interval",
            hours=hours,
            id=JOB_ID,
            next_run_time=utcnow() + FIRST_RUN_DELAY,
            coalesce=True,
            max_instances=1,
        )
        self._sched.start()
        log.info("Update checks every %s hours", hours)

    def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    def reschedule(self, hours: int) -> None:
        if self._sched.running:
            self._sched.reschedule_job(JOB_ID, trigger="interval", hours=hours)
            log.info("Update checks now every %s hours", hours)

    def next_run(self):
        job = self._sched.get_job(JOB_ID) if self._sched.running else None
        return job.next_run_time if job else None

    async def check_all(self) -> int:
        """Queue a check for every active recomp. Returns how many were queued."""
        jobs = self.services.jobs
        with self.services.sessionmaker() as s:
            ids = list(s.scalars(select(Recomp.id).where(Recomp.status == RecompStatus.active)))
            for rid in ids:
                jobs.enqueue(s, CHECK, recomp_id=rid)
        log.info("Scheduled check queued for %d recomps", len(ids))
        return len(ids)
