"""Server-side builds and extractions."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import storage
from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.builder import queue_builds, targets
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.models import Artifact, Build, BuildKind, JobStatus, Recomp
from recompmanager.recipes.store import RecipeError
from recompmanager.services import Services, get_services

router = APIRouter(tags=["builds"])


class BuildOutput(BaseModel):
    artifact_id: int
    name: str
    size: int
    sha256: str | None


class BuildOut(BaseModel):
    id: int
    recomp_id: str
    version: str
    platform: str = Field(description='"any" for an extraction that serves every platform')
    kind: BuildKind
    status: JobStatus
    auto: bool = Field(description="Started by the server itself")
    job_id: int | None
    input_ids: list[int]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    outputs: list[BuildOutput]


class BuildTarget(BaseModel):
    kind: BuildKind
    platform: str
    builder: str
    network: bool
    min_memory_gb: int
    min_disk_gb: int


class BuildsOverview(BaseModel):
    available: bool = Field(description="The server can run builds (Docker reachable)")
    unavailable_reason: str | None
    targets: list[BuildTarget] = Field(description="What this recomp's recipe builds")
    builds: list[BuildOut]


class BuildRequest(BaseModel):
    version: str | None = Field(default=None, description="Default: the current version")
    platform: str | None = Field(
        default=None, description='Default: every target ("any" = extraction)'
    )


def build_out(session: Session, b: Build) -> BuildOut:
    outs = session.scalars(select(Artifact).where(Artifact.build_id == b.id)).all()
    return BuildOut(
        id=b.id,
        recomp_id=b.recomp_id,
        version=b.version_tag,
        platform=b.platform,
        kind=b.kind,
        status=b.status,
        auto=b.auto,
        job_id=b.job_id,
        input_ids=b.input_ids or [],
        error=b.error,
        created_at=b.created_at,
        started_at=b.started_at,
        finished_at=b.finished_at,
        outputs=[
            BuildOutput(artifact_id=a.id, name=a.name, size=a.size, sha256=a.sha256)
            for a in sorted(outs, key=lambda a: a.name)
        ],
    )


def _recomp(session: Session, recomp_id: str) -> Recomp:
    r = session.get(Recomp, recomp_id)
    if r is None:
        raise not_found("Recomp")
    return r


@router.get(
    "/recomps/{recomp_id}/builds",
    response_model=BuildsOverview,
    summary="What the server builds for a recomp, and past builds",
)
def list_builds(
    recomp_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> BuildsOverview:
    _recomp(session, recomp_id)
    try:
        ts = targets(svc.recipes.load(recomp_id))
    except RecipeError:
        ts = []
    reason = svc.builder.unavailable()
    builds = session.scalars(
        select(Build).where(Build.recomp_id == recomp_id).order_by(Build.id.desc()).limit(50)
    ).all()
    return BuildsOverview(
        available=reason is None,
        unavailable_reason=reason,
        targets=[
            BuildTarget(
                kind=t.kind,
                platform=t.platform,
                builder=t.spec.builder,
                network=t.spec.network,
                min_memory_gb=t.spec.min_memory_gb,
                min_disk_gb=t.spec.min_disk_gb,
            )
            for t in ts
        ],
        builds=[build_out(session, b) for b in builds],
    )


@router.post(
    "/recomps/{recomp_id}/builds",
    response_model=list[BuildOut],
    summary="Build or extract now",
    description="Starts builds for the current (or given) version: one platform, or every "
    "target in the recipe. Builds that are already queued or running are left alone.",
)
def start_builds(
    recomp_id: str,
    body: BuildRequest | None = None,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> list[BuildOut]:
    recomp = _recomp(session, recomp_id)
    body = body or BuildRequest()
    if reason := svc.builder.unavailable():
        raise ApiError(409, "builds_unavailable", f"Server builds are off: {reason}")
    if not (body.version or recomp.current_version):
        raise ApiError(409, "no_version", "Download a version first")
    made = queue_builds(
        session, svc, recomp, auto=False, platform=body.platform, version=body.version
    )
    if not made:
        raise ApiError(
            409,
            "nothing_to_build",
            "Nothing to start: the recipe builds nothing for that platform, the version isn't "
            "downloaded, or a build is already running",
        )
    return [build_out(session, b) for b in made]


@router.get("/builds/{build_id}", response_model=BuildOut, summary="Get a build")
def get_build(
    build_id: int,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> BuildOut:
    b = session.get(Build, build_id)
    if b is None:
        raise not_found("Build")
    return build_out(session, b)


@router.get(
    "/builds/{build_id}/log",
    summary="A build's full log (Server-Sent Events, follows a running build)",
    description='Each event is `event: line` with `{"text": ...}`; a final `event: end` '
    'carries `{"status": ...}`.',
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Log lines"}},
)
async def build_log(
    build_id: int,
    request: Request,
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> StreamingResponse:
    with svc.sessionmaker() as s:
        b = s.get(Build, build_id)
        if b is None:
            raise not_found("Build")
        log_rel = b.log_rel

    def status() -> JobStatus:
        with svc.sessionmaker() as s:
            row = s.get(Build, build_id)
            return row.status if row else JobStatus.failed

    async def gen():
        pos = 0
        path = storage.resolve(svc.env.data_dir, log_rel) if log_rel else None
        while True:
            if path is not None and path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    yield f"event: line\ndata: {json.dumps({'text': line})}\n\n"
            st = status()
            if st.finished:
                yield f"event: end\ndata: {json.dumps({'status': str(st)})}\n\n"
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
