"""Database-backed job queue run by asyncio workers in this process.

- Jobs are rows in ``jobs``; workers claim the oldest queued job whose recomp
  or game has nothing else running, so two jobs never touch the same thing.
- Handlers are ``async def handler(ctx: JobContext) -> dict | None`` registered
  with ``@job_handler("kind")``. They report progress and log lines through ctx.
- Cancellation cancels the handler's asyncio task; handlers clean up in
  ``finally`` blocks. CPU-heavy work goes to ``ctx.run_cpu`` (a process pool).
- On startup, jobs left running by a crash or restart are marked failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from recompmanager import events
from recompmanager.events import EventBus
from recompmanager.models import Job, JobStatus, utcnow

log = logging.getLogger(__name__)

MAX_LOG_CHARS = 200_000
_PROGRESS_INTERVAL_S = 0.5

JobHandler = Callable[["JobContext"], Awaitable[dict[str, Any] | None]]
_HANDLERS: dict[str, JobHandler] = {}


def job_handler(kind: str) -> Callable[[JobHandler], JobHandler]:
    def register(fn: JobHandler) -> JobHandler:
        _HANDLERS[kind] = fn
        return fn

    return register


class JobError(Exception):
    """Expected failure: the message is shown to the user as-is."""


def job_event(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "recomp_id": job.recomp_id,
        "game_id": job.game_id,
        "status": str(job.status),
        "progress": job.progress,
        "message": job.message,
    }


def _locks(job: Job) -> set[str]:
    """What a job holds while running; jobs sharing a lock run one at a time."""
    out = set()
    if job.recomp_id:
        out.add(f"recomp:{job.recomp_id}")
    if job.game_id:
        out.add(f"game:{job.game_id}")
    return out


class JobContext:
    def __init__(self, manager: JobManager, job: Job) -> None:
        self.manager = manager
        self.job_id = job.id
        self.kind = job.kind
        self.recomp_id = job.recomp_id
        self.game_id = job.game_id
        self.params: dict[str, Any] = dict(job.params or {})
        self._log_buffer: list[str] = []
        self._last_flush = 0.0
        self._progress: float | None = None
        self._message: str | None = None

    @property
    def services(self) -> Any:
        return self.manager.services

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        with self.manager.sessionmaker() as s:
            yield s
            s.commit()

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log_buffer.append(f"{stamp} {line}")

    def progress(self, fraction: float | None = None, message: str | None = None) -> None:
        """Record progress. Writes are throttled; the final state is always flushed."""
        if fraction is not None:
            self._progress = max(0.0, min(1.0, fraction))
        if message is not None:
            self._message = message[:500]
        if time.monotonic() - self._last_flush >= _PROGRESS_INTERVAL_S:
            self.flush()

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        lines, self._log_buffer = self._log_buffer, []
        with self.session() as s:
            job = s.get(Job, self.job_id)
            if job is None:
                return
            job.progress = self._progress
            job.message = self._message
            if lines:
                job.log = (job.log + "\n".join(lines) + "\n")[-MAX_LOG_CHARS:]
            self.manager.events.publish(events.JOB_UPDATED, job_event(job))

    async def run_cpu(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a picklable function in the process pool (hashing, extraction)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.manager.cpu_pool, fn, *args)


class JobManager:
    def __init__(
        self,
        factory: sessionmaker[Session],
        bus: EventBus,
        services: Any = None,
        workers: int = 2,
    ) -> None:
        self.sessionmaker = factory
        self.events = bus
        self.services = services  # app-level things handlers need (env, http client...)
        self.workers = workers
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._running: dict[int, asyncio.Task[Any]] = {}
        self._cpu_pool: ProcessPoolExecutor | None = None
        self._stopping = False

    @property
    def cpu_pool(self) -> ProcessPoolExecutor:
        if self._cpu_pool is None:
            self._cpu_pool = ProcessPoolExecutor(max_workers=2)
        return self._cpu_pool

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        with self.sessionmaker() as s:
            s.execute(
                update(Job)
                .where(Job.status == JobStatus.running)
                .values(
                    status=JobStatus.failed,
                    error="Interrupted: the server stopped while this job was running",
                    finished_at=utcnow(),
                )
            )
            s.commit()
        self._stopping = False
        self._worker_tasks = [
            asyncio.create_task(self._worker(i), name=f"job-worker-{i}")
            for i in range(self.workers)
        ]
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._running.values()):
            task.cancel()
        for task in self._worker_tasks:
            task.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks = []
        if self._cpu_pool is not None:
            self._cpu_pool.shutdown(wait=False, cancel_futures=True)
            self._cpu_pool = None

    # ---------------------------------------------------------------- public API

    def enqueue(
        self,
        session: Session,
        kind: str,
        *,
        recomp_id: str | None = None,
        game_id: str | None = None,
        params: dict[str, Any] | None = None,
        dedupe: bool = True,
    ) -> Job:
        """Queue a job (or return an identical one already queued/running).

        Commits the caller's session so workers can see the job right away.
        """
        if kind not in _HANDLERS:
            raise ValueError(f"Unknown job kind: {kind}")
        params = params or {}
        if dedupe:
            existing = session.scalars(
                select(Job).where(
                    Job.kind == kind,
                    Job.recomp_id == recomp_id,
                    Job.game_id == game_id,
                    Job.status.in_([JobStatus.queued, JobStatus.running]),
                )
            ).all()
            for job in existing:
                if (job.params or {}) == params:
                    return job
        job = Job(
            kind=kind,
            recomp_id=recomp_id,
            game_id=game_id,
            params=params,
            status=JobStatus.queued,
            log="",
        )
        session.add(job)
        session.commit()
        self.events.publish(events.JOB_UPDATED, job_event(job))
        self.wake()
        return job

    def wake(self) -> None:
        loop = self.events._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake.set)

    def request_cancel(self, session: Session, job: Job) -> None:
        if job.status.finished:
            return
        if job.status == JobStatus.queued:
            job.status = JobStatus.cancelled
            job.finished_at = utcnow()
            job.message = "Cancelled before it started"
            self.events.publish(events.JOB_UPDATED, job_event(job))
            return
        job.cancel_requested = True
        task = self._running.get(job.id)
        if task is not None:
            loop = self.events._loop
            if loop is not None:
                loop.call_soon_threadsafe(task.cancel)

    async def wait_idle(self, timeout: float = 30) -> None:
        """Test helper: wait until no job is queued or running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.sessionmaker() as s:
                busy = s.scalar(
                    select(Job.id).where(Job.status.in_([JobStatus.queued, JobStatus.running]))
                )
            if busy is None and not self._running:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError("jobs still running")

    # ---------------------------------------------------------------- workers

    async def _claim(self) -> Job | None:
        async with self._claim_lock:
            with self.sessionmaker() as s:
                busy: set[str] = set()
                for running in s.scalars(select(Job).where(Job.status == JobStatus.running)):
                    busy |= _locks(running)
                for job in s.scalars(
                    select(Job).where(Job.status == JobStatus.queued).order_by(Job.id)
                ):
                    if _locks(job) & busy:
                        continue
                    job.status = JobStatus.running
                    job.started_at = utcnow()
                    s.commit()
                    s.refresh(job)
                    s.expunge(job)
                    return job
        return None

    async def _worker(self, n: int) -> None:
        while not self._stopping:
            job = await self._claim()
            if job is None:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                continue
            self.events.publish(events.JOB_UPDATED, job_event(job))
            await self._run(job)
            self._wake.set()  # a finished job may unblock another for the same thing

    async def _run(self, job: Job) -> None:
        ctx = JobContext(self, job)
        handler = _HANDLERS.get(job.kind)
        status, error, result = JobStatus.succeeded, None, None
        task = asyncio.current_task()
        assert task is not None
        inner = asyncio.create_task(handler(ctx)) if handler else None
        if inner is not None:
            self._running[job.id] = inner
        try:
            if inner is None:
                raise JobError(f"No handler for job kind {job.kind!r}")
            result = await inner
        except asyncio.CancelledError:
            if self._stopping:
                status, error = JobStatus.failed, "Interrupted: the server is shutting down"
            else:
                status = JobStatus.cancelled
        except JobError as e:
            status, error = JobStatus.failed, str(e)
            ctx.log(f"Failed: {e}")
        except Exception as e:  # unexpected: keep the traceback in the server log
            log.exception("Job %s (%s) crashed", job.id, job.kind)
            status, error = JobStatus.failed, f"Unexpected error: {e}"
            ctx.log(f"Unexpected error: {e!r}")
        finally:
            self._running.pop(job.id, None)
        if status == JobStatus.succeeded:
            ctx._progress = 1.0
        ctx.flush()
        with self.sessionmaker() as s:
            row = s.get(Job, job.id)
            if row is not None:
                row.status = status
                row.error = error
                row.result = result
                row.finished_at = utcnow()
                if status == JobStatus.cancelled:
                    row.message = "Cancelled"
                s.commit()
                self.events.publish(events.JOB_UPDATED, job_event(row))
