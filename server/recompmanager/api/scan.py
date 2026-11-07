"""ROM library scan and its suggestions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import events
from recompmanager.api.games import get_game_or_404
from recompmanager.api.jobs import JobSummary, job_summary
from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.models import CandidateState, ScanCandidate
from recompmanager.scan import SCAN, accept
from recompmanager.services import Services, get_services

router = APIRouter(tags=["scan"])


class SuggestionOut(BaseModel):
    id: int
    game_id: str
    path: str = Field(description="Where it is, inside the container (/roms/...)")
    member: str = Field(description="Entry inside an archive, or empty")
    kind: Literal["file", "folder", "archive_member", "package"]
    confidence: Literal["verified", "likely", "unusable"] = Field(
        description="verified: checksum or title ID matches; likely: name or layout fits; "
        "unusable: an encrypted package that can't be used as-is"
    )
    reason: str
    size: int
    state: CandidateState
    input_id: int | None
    found_at: datetime


class AcceptRequest(BaseModel):
    mode: Literal["reference", "copy"] = Field(
        default="reference",
        description="reference reads it in place; copy puts a copy in the vault. "
        "An archive entry is always extracted into the vault.",
    )


def _out(c: ScanCandidate) -> SuggestionOut:
    return SuggestionOut(
        id=c.id,
        game_id=c.game_id,
        path=c.path,
        member=c.member,
        kind=c.kind,  # type: ignore[arg-type]
        confidence=c.confidence,  # type: ignore[arg-type]
        reason=c.reason,
        size=c.size,
        state=c.state,
        input_id=c.input_id,
        found_at=c.found_at,
    )


@router.post(
    "/system/scan",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Scan the ROM library (/roms and /import) for inputs",
    tags=["system"],
)
def start_scan(
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    return job_summary(svc.jobs.enqueue(session, SCAN))


@router.get(
    "/games/{game_id}/suggestions",
    response_model=list[SuggestionOut],
    summary="What the scan found for a game",
)
def list_suggestions(
    game_id: str,
    include_handled: bool = False,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> list[SuggestionOut]:
    get_game_or_404(session, game_id)
    q = select(ScanCandidate).where(ScanCandidate.game_id == game_id)
    if not include_handled:
        q = q.where(ScanCandidate.state == CandidateState.new)
    rank = {"verified": 0, "likely": 1, "unusable": 2}
    rows = sorted(session.scalars(q), key=lambda c: (rank.get(c.confidence, 3), c.path))
    return [_out(c) for c in rows]


def _candidate(session: Session, cid: int) -> ScanCandidate:
    c = session.get(ScanCandidate, cid)
    if c is None:
        raise not_found("Suggestion")
    return c


@router.post(
    "/suggestions/{suggestion_id}/accept",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Use a suggestion as an input",
)
def accept_suggestion(
    suggestion_id: int,
    body: AcceptRequest | None = None,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    c = _candidate(session, suggestion_id)
    if c.confidence == "unusable":
        raise ApiError(409, "unusable", c.reason)
    if c.state == CandidateState.accepted:
        raise ApiError(409, "already_accepted", "This suggestion was already used")
    return job_summary(accept(svc.jobs, session, c, mode=(body or AcceptRequest()).mode))


@router.post(
    "/suggestions/{suggestion_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hide a suggestion (later scans won't bring it back)",
)
def dismiss_suggestion(
    suggestion_id: int,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> None:
    c = _candidate(session, suggestion_id)
    c.state = CandidateState.dismissed
    session.commit()
    svc.events.publish(events.GAME_UPDATED, {"id": c.game_id})
