from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.db import get_session
from recompmanager.errors import not_found
from recompmanager.models import Job, JobStatus
from recompmanager.services import Services, get_services

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobSummary(BaseModel):
    id: int
    kind: str = Field(description="check | download")
    game_id: str | None
    status: JobStatus
    progress: float | None = Field(description="0..1, or null when unknown")
    message: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobDetail(JobSummary):
    params: dict[str, Any]
    result: dict[str, Any] | None
    log: str


def job_summary(job: Job) -> JobSummary:
    return JobSummary(
        id=job.id,
        kind=job.kind,
        game_id=job.game_id,
        status=job.status,
        progress=job.progress,
        message=job.message,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def job_detail(job: Job) -> JobDetail:
    return JobDetail(
        **job_summary(job).model_dump(), params=job.params, result=job.result, log=job.log
    )


@router.get("", response_model=list[JobSummary], summary="List jobs, newest first")
def list_jobs(
    game_id: str | None = None,
    status: JobStatus | None = None,
    active: bool = Query(default=False, description="Only queued and running jobs"),
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> list[JobSummary]:
    q = select(Job).order_by(Job.id.desc()).limit(limit)
    if game_id:
        q = q.where(Job.game_id == game_id)
    if status:
        q = q.where(Job.status == status)
    if active:
        q = q.where(Job.status.in_([JobStatus.queued, JobStatus.running]))
    return [job_summary(j) for j in session.scalars(q)]


@router.get("/{job_id}", response_model=JobDetail, summary="Get a job with its log")
def get_job(
    job_id: int,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> JobDetail:
    job = session.get(Job, job_id)
    if job is None:
        raise not_found("Job")
    return job_detail(job)


@router.post("/{job_id}/cancel", response_model=JobDetail, summary="Cancel a job")
def cancel_job(
    job_id: int,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobDetail:
    job = session.get(Job, job_id)
    if job is None:
        raise not_found("Job")
    svc.jobs.request_cancel(session, job)
    session.commit()
    return job_detail(job)
