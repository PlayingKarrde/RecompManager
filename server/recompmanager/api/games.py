"""Games: the original titles. Recomps run them; inputs (the user's dumps) belong to them."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from recompmanager import events
from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.library import update_available
from recompmanager.models import CandidateState, Game, InputState, RecompStatus, ScanCandidate
from recompmanager.recipes.store import RecipeError
from recompmanager.services import Services, get_services

router = APIRouter(prefix="/games", tags=["games"])


class GameRecomp(BaseModel):
    id: str
    name: str
    status: RecompStatus
    current_version: str | None
    update_available: bool


class GameSummary(BaseModel):
    id: str
    title: str
    system: str | None
    identifiers: dict[str, str]
    recomps: list[GameRecomp]
    inputs_ready: int = Field(description="Inputs stored and readable")
    suggestions: int = Field(description="New matches from the ROM library scan")
    created_at: datetime


class GamePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    system: str | None = Field(default=None, max_length=50)
    identifiers: dict[str, str] | None = None


def game_summary(session: Session, game: Game, svc: Services) -> GameSummary:
    recomps = []
    for r in sorted(game.recomps, key=lambda r: r.id):
        try:
            name = svc.recipes.load(r.id).name
        except RecipeError:
            name = r.id
        recomps.append(
            GameRecomp(
                id=r.id,
                name=name,
                status=r.status,
                current_version=r.current_version,
                update_available=update_available(r),
            )
        )
    suggestions = session.scalar(
        select(func.count())
        .select_from(ScanCandidate)
        .where(ScanCandidate.game_id == game.id, ScanCandidate.state == CandidateState.new)
    )
    return GameSummary(
        id=game.id,
        title=game.title,
        system=game.system,
        identifiers=game.identifiers or {},
        recomps=recomps,
        inputs_ready=sum(1 for i in game.inputs if i.state == InputState.ready),
        suggestions=suggestions or 0,
        created_at=game.created_at,
    )


def get_game_or_404(session: Session, game_id: str) -> Game:
    game = session.get(Game, game_id)
    if game is None:
        raise not_found("Game")
    return game


@router.get("", response_model=list[GameSummary], summary="List games")
def list_games(
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> list[GameSummary]:
    games = session.scalars(select(Game).order_by(Game.title)).all()
    return [game_summary(session, g, svc) for g in games]


@router.get("/{game_id}", response_model=GameSummary, summary="Get a game")
def get_game(
    game_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> GameSummary:
    return game_summary(session, get_game_or_404(session, game_id), svc)


@router.patch("/{game_id}", response_model=GameSummary, summary="Rename or describe a game")
def update_game(
    game_id: str,
    body: GamePatch,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> GameSummary:
    game = get_game_or_404(session, game_id)
    if body.title is not None:
        game.title = body.title.strip()
    if body.system is not None:
        game.system = body.system.strip() or None
    if body.identifiers is not None:
        game.identifiers = body.identifiers
    session.commit()
    # Keep recipe files in step, so a hand-read recipe shows the same game.
    for r in game.recomps:
        try:
            recipe = svc.recipes.load(r.id)
        except RecipeError:
            continue
        if recipe.game and recipe.game.id == game.id:
            ref = recipe.game.model_copy(
                update={
                    "title": game.title,
                    "system": game.system,
                    "identifiers": game.identifiers or {},
                }
            )
            r.recipe_hash = svc.recipes.save(recipe.model_copy(update={"game": ref}))
    session.commit()
    svc.events.publish(events.GAME_UPDATED, {"id": game.id})
    return game_summary(session, game, svc)


@router.delete(
    "/{game_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a game with no recomps and no inputs",
)
def delete_game(
    game_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> None:
    game = get_game_or_404(session, game_id)
    if game.recomps:
        raise ApiError(
            409, "in_use", "Recomps still point at this game. Move or remove them first."
        )
    if game.inputs:
        raise ApiError(409, "in_use", "This game still has inputs. Remove them first.")
    session.delete(game)
    session.commit()
    svc.events.publish(events.GAME_UPDATED, {"id": game_id})
