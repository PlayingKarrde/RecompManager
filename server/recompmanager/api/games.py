from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import events, storage
from recompmanager.api.jobs import JobSummary, job_summary
from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.github import SourceError, newest_on_channel, parse_repo
from recompmanager.library import (
    CHECK,
    DOWNLOAD,
    delete_version_files,
    update_available,
    upsert_versions,
)
from recompmanager.models import (
    Artifact,
    ArtifactState,
    Game,
    GameStatus,
    Job,
    JobStatus,
    Version,
    VersionState,
)
from recompmanager.recipes.matching import guess_rules, match_assets
from recompmanager.recipes.schema import AssetRule, Recipe, Source
from recompmanager.recipes.store import RecipeError
from recompmanager.services import Services, get_services

router = APIRouter(tags=["games"])


# ---------------------------------------------------------------- schemas


class RecipeSummary(BaseModel):
    id: str
    name: str
    repo: str
    description: str


class GameSummary(BaseModel):
    id: str
    name: str
    status: GameStatus
    repo: str | None
    channel: Literal["stable", "prerelease"] | None
    auto_update: bool
    current_version: str | None
    latest_version: str | None
    update_available: bool
    last_checked_at: datetime | None
    last_check_error: str | None
    platforms: list[str] = Field(description="Platform keys the recipe has asset rules for")
    recipe_error: str | None = Field(description="Set when the recipe file can't be loaded")


class GameDetail(GameSummary):
    recipe: Recipe | None
    latest_assets: dict[str, str] = Field(
        description="Platform key → asset name in the newest known release"
    )
    unmatched_assets: list[str] = Field(
        description="Assets in the newest known release that no rule matches"
    )


class GameCreate(BaseModel):
    repo_url: str | None = Field(default=None, description="GitHub URL or owner/name")
    recipe_id: str | None = Field(default=None, description="Id of a built-in recipe")

    @model_validator(mode="after")
    def _one(self) -> GameCreate:
        if bool(self.repo_url) == bool(self.recipe_id):
            raise ValueError("Give either repo_url or recipe_id")
        return self


class GamePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    channel: Literal["stable", "prerelease"] | None = None
    auto_update: bool | None = None
    assets: dict[str, AssetRule] | None = Field(
        default=None, description="Replaces all asset rules"
    )
    recipe: Recipe | None = Field(default=None, description="Replaces the whole recipe")
    status: Literal["active"] | None = Field(
        default=None, description="Set to active to confirm a draft"
    )


class ArtifactOut(BaseModel):
    id: int
    name: str
    platforms: list[str]
    size: int
    sha256: str | None
    state: ArtifactState
    downloaded_at: datetime | None


class VersionOut(BaseModel):
    tag: str
    name: str
    published_at: datetime | None
    prerelease: bool
    pinned: bool
    current: bool
    state: VersionState
    downloaded_at: datetime | None
    url: str | None
    notes: str = Field(description="Release notes (markdown)")
    platforms: list[str] = Field(description="Platforms this version has assets for")
    size: int = Field(description="Total size of matched assets in bytes")


class UpdateRequest(BaseModel):
    version: str | None = Field(default=None, description="Tag to download; default newest")
    make_current: bool | None = Field(
        default=None, description="Default: yes if it's newer than the current version"
    )


# ---------------------------------------------------------------- helpers


def _load_recipe(svc: Services, game_id: str) -> tuple[Recipe | None, str | None]:
    try:
        return svc.recipes.load(game_id), None
    except RecipeError as e:
        return None, str(e)


def _summary(game: Game, recipe: Recipe | None, err: str | None) -> dict:
    return {
        "id": game.id,
        "name": recipe.name if recipe else game.id,
        "status": game.status,
        "repo": recipe.source.repo if recipe else None,
        "channel": recipe.source.channel if recipe else None,
        "auto_update": recipe.source.auto_update if recipe else False,
        "current_version": game.current_version,
        "latest_version": game.latest_version,
        "update_available": update_available(game),
        "last_checked_at": game.last_checked_at,
        "last_check_error": game.last_check_error,
        "platforms": sorted(recipe.assets) if recipe else [],
        "recipe_error": err,
    }


def _newest(game: Game, recipe: Recipe | None) -> Version | None:
    if game.latest_version:
        for v in game.versions:
            if v.tag == game.latest_version:
                return v
    channel = recipe.source.channel if recipe else "prerelease"
    cands = [v for v in game.versions if channel == "prerelease" or not v.prerelease]
    return max(cands, key=lambda v: (v.published_at is not None, v.published_at), default=None)


def _detail(game: Game, svc: Services) -> GameDetail:
    recipe, err = _load_recipe(svc, game.id)
    newest = _newest(game, recipe)
    latest_assets: dict[str, str] = {}
    unmatched: list[str] = []
    if newest and recipe:
        latest_assets = match_assets(recipe.assets, newest.assets)
        used = set(latest_assets.values())
        unmatched = [a["name"] for a in newest.assets if a["name"] not in used]
    return GameDetail(
        **_summary(game, recipe, err),
        recipe=recipe,
        latest_assets=latest_assets,
        unmatched_assets=unmatched,
    )


def _game(session: Session, game_id: str) -> Game:
    game = session.get(Game, game_id)
    if game is None:
        raise not_found("Game")
    return game


def _version(session: Session, game_id: str, tag: str) -> Version:
    v = session.scalar(select(Version).where(Version.game_id == game_id, Version.tag == tag))
    if v is None:
        raise not_found("Version")
    return v


def _version_out(game: Game, v: Version, recipe: Recipe | None) -> VersionOut:
    matched = match_assets(recipe.assets, v.assets) if recipe else {}
    sizes = {a["name"]: int(a.get("size") or 0) for a in v.assets}
    return VersionOut(
        tag=v.tag,
        name=v.name,
        published_at=v.published_at,
        prerelease=v.prerelease,
        pinned=v.pinned,
        current=v.tag == game.current_version,
        state=v.state,
        downloaded_at=v.downloaded_at,
        url=v.url,
        notes=v.notes,
        platforms=sorted(matched),
        size=sum(sizes.get(n, 0) for n in set(matched.values())),
    )


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "game"


def _pretty_name(repo_name: str) -> str:
    words = re.sub(r"[-_]+", " ", repo_name).strip()
    return words[:1].upper() + words[1:] if words else repo_name


def _source_error(e: SourceError) -> ApiError:
    return ApiError(502, "source_error", str(e))


# ---------------------------------------------------------------- recipes catalog


@router.get(
    "/recipes",
    response_model=list[RecipeSummary],
    summary="List built-in recipes",
    tags=["recipes"],
)
def list_recipes(
    svc: Services = Depends(get_services), _p: Principal = Depends(require_token)
) -> list[RecipeSummary]:
    return [
        RecipeSummary(id=r.id, name=r.name, repo=r.source.repo, description=r.description)
        for r in svc.recipes.builtins().values()
    ]


# ---------------------------------------------------------------- games


@router.get("/games", response_model=list[GameSummary], summary="List games")
def list_games(
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> list[GameSummary]:
    out = []
    for g in session.scalars(select(Game)):
        recipe, err = _load_recipe(svc, g.id)
        out.append(GameSummary(**_summary(g, recipe, err)))
    return sorted(out, key=lambda g: g.name.lower())


@router.post(
    "/games",
    response_model=GameDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Add a game",
    description="From a built-in recipe (by id, or automatically when the repo has one), or "
    "from any GitHub repo URL. A repo without a built-in recipe becomes a **draft**: asset rules "
    "are guessed from the newest release; check them, then PATCH status=active.",
)
async def create_game(
    body: GameCreate,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> GameDetail:
    releases: list[dict] | None = None
    if body.recipe_id:
        recipe = svc.recipes.builtins().get(body.recipe_id)
        if recipe is None:
            raise not_found("Recipe")
    else:
        try:
            repo = parse_repo(body.repo_url or "")
        except ValueError as e:
            raise ApiError(422, "invalid_repo", str(e)) from e
        recipe = svc.recipes.builtin_for_repo(repo)
        if recipe is None:
            try:
                info = await svc.github.repo(repo)
                releases = await svc.github.releases(repo)
            except SourceError as e:
                raise _source_error(e) from e
            full = info.get("full_name") or repo
            name = full.split("/", 1)[1]
            channel = "stable" if newest_on_channel(releases, "stable") else "prerelease"
            rules, _unmatched = guess_rules(
                [r for r in releases if channel == "prerelease" or not r["prerelease"]]
            )
            recipe = Recipe(
                id=_slug(name),
                name=_pretty_name(name),
                description=(info.get("description") or "")[:500],
                source=Source(repo=full, channel=channel),
                assets=rules,
                unverified=["assets", "input", "platforms", "launch", "saves"],
                notes="Draft created from the repository's releases. Check the asset rules.",
            )

    for g in session.scalars(select(Game)):
        other, _ = _load_recipe(svc, g.id)
        if other and other.source.repo.lower() == recipe.source.repo.lower():
            raise ApiError(409, "conflict", f"This repo is already in the library as {g.id!r}")
    base, n = recipe.id, 2
    while session.get(Game, recipe.id) is not None or svc.recipes.exists(recipe.id):
        recipe = recipe.model_copy(update={"id": f"{base}-{n}"})
        n += 1

    is_draft = releases is not None
    svc.recipes.save(recipe)
    game = Game(id=recipe.id, status=GameStatus.draft if is_draft else GameStatus.active)
    session.add(game)
    if releases is not None:
        upsert_versions(session, game, releases)
        newest = newest_on_channel(releases, recipe.source.channel)
        game.latest_version = newest["tag"] if newest else None
    session.commit()
    if not is_draft:
        # Fetch releases now, but let the user start the first download.
        svc.jobs.enqueue(session, CHECK, game.id, {"auto_download": False})
    svc.events.publish(events.GAME_UPDATED, {"id": game.id})
    session.refresh(game)
    return _detail(game, svc)


@router.get("/games/{game_id}", response_model=GameDetail, summary="Get a game")
def get_game(
    game_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> GameDetail:
    return _detail(_game(session, game_id), svc)


@router.patch("/games/{game_id}", response_model=GameDetail, summary="Change a game")
def update_game(
    game_id: str,
    body: GamePatch,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> GameDetail:
    game = _game(session, game_id)
    recipe, err = _load_recipe(svc, game_id)
    if body.recipe is not None:
        if body.recipe.id != game_id:
            raise ApiError(422, "validation_error", "recipe.id must match the game id")
        recipe = body.recipe
    elif recipe is None:
        raise ApiError(409, "recipe_error", f"Fix the recipe file first: {err}")
    data = recipe.model_dump()
    if body.name is not None:
        data["name"] = body.name
    if body.assets is not None:
        data["assets"] = {k: rule.model_dump() for k, rule in body.assets.items()}
    if body.channel is not None:
        data["source"]["channel"] = body.channel
    if body.auto_update is not None:
        data["source"]["auto_update"] = body.auto_update
    channel_changed = data["source"]["channel"] != recipe.source.channel
    try:
        updated = Recipe.model_validate(data)
    except ValidationError as e:
        details = [
            {"loc": ["body", *e_["loc"]], "msg": e_["msg"], "type": e_["type"]} for e_ in e.errors()
        ]
        raise ApiError(422, "validation_error", "The recipe isn't valid", details) from e
    svc.recipes.save(updated)

    confirm = body.status == "active" and game.status == GameStatus.draft
    if confirm:
        game.status = GameStatus.active
    session.commit()
    if confirm or channel_changed:
        svc.jobs.enqueue(session, CHECK, game.id, {"auto_download": False})
    svc.events.publish(events.GAME_UPDATED, {"id": game.id})
    session.refresh(game)
    return _detail(game, svc)


@router.delete("/games/{game_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a game")
def delete_game(
    game_id: str,
    delete_files: bool = Query(default=True, description="Also delete downloaded files"),
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> None:
    game = _game(session, game_id)
    busy = session.scalar(
        select(Job.id).where(
            Job.game_id == game_id, Job.status.in_([JobStatus.queued, JobStatus.running])
        )
    )
    if busy is not None:
        raise ApiError(
            409, "busy", "A job for this game is still queued or running. Cancel it first."
        )
    if delete_files:
        storage.remove_tree(
            storage.resolve(svc.env.data_dir, f"library/{storage.safe_component(game_id)}")
        )
    session.delete(game)
    session.commit()
    svc.recipes.delete(game_id)
    svc.events.publish(events.GAME_UPDATED, {"id": game_id})


@router.post(
    "/games/{game_id}/check",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Check for new releases",
)
def check_game(
    game_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    _game(session, game_id)
    return job_summary(svc.jobs.enqueue(session, CHECK, game_id, {"auto_download": False}))


@router.post(
    "/games/{game_id}/update",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Download a version (default: the newest)",
)
def download_game(
    game_id: str,
    body: UpdateRequest | None = None,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    game = _game(session, game_id)
    if game.status == GameStatus.draft:
        raise ApiError(409, "draft", "Confirm the draft recipe before downloading")
    body = body or UpdateRequest()
    if body.version:
        _version(session, game_id, body.version)
    elif not game.latest_version:
        raise ApiError(409, "no_version", "No release known yet. Check for updates first.")
    params = {"version": body.version or game.latest_version}
    if body.make_current is not None:
        params["make_current"] = body.make_current
    return job_summary(svc.jobs.enqueue(session, DOWNLOAD, game_id, params))


# ---------------------------------------------------------------- versions


@router.get(
    "/games/{game_id}/versions",
    response_model=list[VersionOut],
    summary="List versions, newest first",
    tags=["versions"],
)
def list_versions(
    game_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> list[VersionOut]:
    game = _game(session, game_id)
    recipe, _ = _load_recipe(svc, game_id)
    vs = sorted(
        game.versions, key=lambda v: (v.published_at is not None, v.published_at), reverse=True
    )
    return [_version_out(game, v, recipe) for v in vs]


@router.get(
    "/games/{game_id}/versions/{version}",
    response_model=VersionOut,
    summary="Get a version",
    tags=["versions"],
)
def get_version(
    game_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> VersionOut:
    game = _game(session, game_id)
    recipe, _ = _load_recipe(svc, game_id)
    return _version_out(game, _version(session, game_id, version), recipe)


def _set_pin(session: Session, svc: Services, game_id: str, tag: str, pinned: bool) -> VersionOut:
    game = _game(session, game_id)
    v = _version(session, game_id, tag)
    v.pinned = pinned
    session.commit()
    recipe, _ = _load_recipe(svc, game_id)
    return _version_out(game, v, recipe)


@router.post(
    "/games/{game_id}/versions/{version}/pin",
    response_model=VersionOut,
    summary="Pin a version (never pruned)",
    tags=["versions"],
)
def pin_version(
    game_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    return _set_pin(session, svc, game_id, version, True)


@router.delete(
    "/games/{game_id}/versions/{version}/pin",
    response_model=VersionOut,
    summary="Unpin a version",
    tags=["versions"],
)
def unpin_version(
    game_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    return _set_pin(session, svc, game_id, version, False)


@router.post(
    "/games/{game_id}/versions/{version}/activate",
    response_model=VersionOut,
    summary="Make this the current version (rollback or roll forward)",
    tags=["versions"],
)
def activate_version(
    game_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    game = _game(session, game_id)
    v = _version(session, game_id, version)
    if v.state != VersionState.downloaded:
        raise ApiError(409, "not_downloaded", "Download this version before making it current")
    game.current_version = v.tag
    session.commit()
    svc.events.publish(events.GAME_UPDATED, {"id": game_id})
    recipe, _ = _load_recipe(svc, game_id)
    return _version_out(game, v, recipe)


@router.delete(
    "/games/{game_id}/versions/{version}",
    response_model=VersionOut,
    summary="Delete a version's downloaded files",
    description="The version stays listed and can be downloaded again.",
    tags=["versions"],
)
def delete_version(
    game_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    game = _game(session, game_id)
    v = _version(session, game_id, version)
    if v.tag == game.current_version:
        raise ApiError(
            409, "current", "This is the current version. Make another one current first."
        )
    if v.pinned:
        raise ApiError(409, "pinned", "Unpin this version before deleting its files")
    if v.state == VersionState.downloading:
        raise ApiError(409, "busy", "This version is downloading")
    delete_version_files(session, svc.env.data_dir, v)
    session.commit()
    recipe, _ = _load_recipe(svc, game_id)
    return _version_out(game, v, recipe)


# ---------------------------------------------------------------- artifacts


def _artifact_out(a: Artifact) -> ArtifactOut:
    return ArtifactOut(
        id=a.id,
        name=a.name,
        platforms=a.platforms,
        size=a.size,
        sha256=a.sha256,
        state=a.state,
        downloaded_at=a.downloaded_at,
    )


@router.get(
    "/games/{game_id}/versions/{version}/artifacts",
    response_model=list[ArtifactOut],
    summary="List a version's stored files",
    tags=["artifacts"],
)
def list_artifacts(
    game_id: str,
    version: str,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> list[ArtifactOut]:
    v = _version(session, game_id, version)
    return [_artifact_out(a) for a in sorted(v.artifacts, key=lambda a: a.name)]


@router.get(
    "/artifacts/{artifact_id}", response_model=ArtifactOut, summary="Get a file", tags=["artifacts"]
)
def get_artifact(
    artifact_id: int,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> ArtifactOut:
    a = session.get(Artifact, artifact_id)
    if a is None:
        raise not_found("Artifact")
    return _artifact_out(a)


@router.get(
    "/artifacts/{artifact_id}/download",
    summary="Download a file (supports Range requests)",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "The file"},
        206: {"content": {"application/octet-stream": {}}, "description": "Part of the file"},
    },
    tags=["artifacts"],
)
def download_artifact(
    artifact_id: int,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> FileResponse:
    a = session.get(Artifact, artifact_id)
    if a is None:
        raise not_found("Artifact")
    path = storage.resolve(svc.env.data_dir, a.rel_path)
    if a.state != ArtifactState.downloaded or not path.is_file():
        raise ApiError(409, "not_downloaded", "This file isn't stored on the server")
    return FileResponse(
        path,
        filename=a.name,
        media_type="application/octet-stream",
        headers={"ETag": f'"{a.sha256}"'} if a.sha256 else None,
    )
