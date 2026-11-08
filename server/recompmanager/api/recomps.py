from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from recompmanager import events, storage
from recompmanager.api.jobs import JobSummary, job_summary
from recompmanager.auth import Principal, require_admin, require_token
from recompmanager.db import get_session
from recompmanager.errors import ApiError, not_found
from recompmanager.github import SourceError, newest_on_channel, parse_repo
from recompmanager.library import (
    CHECK,
    DOWNLOAD,
    RELOCATE,
    builtin_status,
    delete_version_files,
    reset_to_builtin,
    sync_recomp_game,
    update_available,
    upsert_versions,
)
from recompmanager.models import (
    Artifact,
    ArtifactState,
    Game,
    Job,
    JobStatus,
    Recomp,
    RecompStatus,
    Version,
    VersionState,
)
from recompmanager.recipes.gameguess import guess_game_title, slugify
from recompmanager.recipes.matching import guess_rules, match_assets
from recompmanager.recipes.schema import AssetRule, GameRef, Recipe, Source
from recompmanager.recipes.store import RecipeError
from recompmanager.services import Services, get_services
from recompmanager.steamos import SteamOSStatus, steamos_status

router = APIRouter(tags=["recomps"])


# ---------------------------------------------------------------- schemas


class RecipeSummary(BaseModel):
    id: str
    name: str
    repo: str
    description: str


class RecompSummary(BaseModel):
    id: str
    name: str
    game_id: str | None = Field(description="The game this recomp runs; null if not known yet")
    game_title: str | None
    status: RecompStatus
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
    steamos: SteamOSStatus = Field(
        description="Whether it installs and runs on SteamOS (Steam Deck), and what's in the way"
    )


class RecompDetail(RecompSummary):
    recipe: Recipe | None
    latest_assets: dict[str, str] = Field(
        description="Platform key → asset name in the newest known release"
    )
    unmatched_assets: list[str] = Field(
        description="Assets in the newest known release that no rule matches"
    )
    builtin_update: bool = Field(
        description="A built-in recipe for this repo differs from the library copy; "
        "POST reset-recipe to take it"
    )


class RecompCreate(BaseModel):
    repo_url: str | None = Field(default=None, description="GitHub URL or owner/name")
    recipe_id: str | None = Field(default=None, description="Id of a built-in recipe")
    game_id: str | None = Field(
        default=None, description="Attach to this existing game instead of the recipe's or a guess"
    )

    @model_validator(mode="after")
    def _one(self) -> RecompCreate:
        if bool(self.repo_url) == bool(self.recipe_id):
            raise ValueError("Give either repo_url or recipe_id")
        return self


class RecompPatch(BaseModel):
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
    game_id: str | None = Field(default=None, description="Attach to an existing game")
    game_title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Attach to the game with this title, creating it if needed",
    )


class ArtifactOut(BaseModel):
    id: int
    name: str
    platform: str
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


def _load_recipe(svc: Services, recomp_id: str) -> tuple[Recipe | None, str | None]:
    try:
        return svc.recipes.load(recomp_id), None
    except RecipeError as e:
        return None, str(e)


def _summary(recomp: Recomp, recipe: Recipe | None, err: str | None, svc: Services) -> dict:
    session = object_session(recomp)
    assert session is not None
    return {
        "id": recomp.id,
        "name": recipe.name if recipe else recomp.id,
        "game_id": recomp.game_id,
        "game_title": recomp.game.title if recomp.game else None,
        "status": recomp.status,
        "repo": recipe.source.repo if recipe else None,
        "channel": recipe.source.channel if recipe else None,
        "auto_update": recipe.source.auto_update if recipe else False,
        "current_version": recomp.current_version,
        "latest_version": recomp.latest_version,
        "update_available": update_available(recomp),
        "last_checked_at": recomp.last_checked_at,
        "last_check_error": recomp.last_check_error,
        "platforms": sorted(recipe.assets) if recipe else [],
        "recipe_error": err,
        "steamos": steamos_status(session, svc.env, svc.builder.unavailable(), recomp, recipe),
    }


def _newest(recomp: Recomp, recipe: Recipe | None) -> Version | None:
    if recomp.latest_version:
        for v in recomp.versions:
            if v.tag == recomp.latest_version:
                return v
    channel = recipe.source.channel if recipe else "prerelease"
    cands = [v for v in recomp.versions if channel == "prerelease" or not v.prerelease]
    return max(cands, key=lambda v: (v.published_at is not None, v.published_at), default=None)


def _detail(recomp: Recomp, svc: Services) -> RecompDetail:
    recipe, err = _load_recipe(svc, recomp.id)
    newest = _newest(recomp, recipe)
    latest_assets: dict[str, str] = {}
    unmatched: list[str] = []
    if newest and recipe:
        latest_assets = match_assets(recipe.assets, newest.assets)
        used = set(latest_assets.values())
        unmatched = [a["name"] for a in newest.assets if a["name"] not in used]
    _builtin, differs = builtin_status(svc.recipes, recomp)
    return RecompDetail(
        **_summary(recomp, recipe, err, svc),
        recipe=recipe,
        latest_assets=latest_assets,
        unmatched_assets=unmatched,
        builtin_update=differs,
    )


def _recomp(session: Session, recomp_id: str) -> Recomp:
    recomp = session.get(Recomp, recomp_id)
    if recomp is None:
        raise not_found("Recomp")
    return recomp


def _version(session: Session, recomp_id: str, tag: str) -> Version:
    v = session.scalar(select(Version).where(Version.recomp_id == recomp_id, Version.tag == tag))
    if v is None:
        raise not_found("Version")
    return v


def _version_out(recomp: Recomp, v: Version, recipe: Recipe | None) -> VersionOut:
    matched = match_assets(recipe.assets, v.assets) if recipe else {}
    sizes = {a["name"]: int(a.get("size") or 0) for a in v.assets}
    return VersionOut(
        tag=v.tag,
        name=v.name,
        published_at=v.published_at,
        prerelease=v.prerelease,
        pinned=v.pinned,
        current=v.tag == recomp.current_version,
        state=v.state,
        downloaded_at=v.downloaded_at,
        url=v.url,
        notes=v.notes,
        platforms=sorted(matched),
        size=sum(sizes.get(n, 0) for n in set(matched.values())),
    )


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


def _game_ref(session: Session, game_id: str | None, title: str | None) -> GameRef | None:
    """A recipe game block for an existing game id, or for a title (existing or new)."""
    if game_id:
        g = session.get(Game, game_id)
        if g is None:
            raise not_found("Game")
        return GameRef(id=g.id, title=g.title, system=g.system, identifiers=g.identifiers or {})
    if title:
        title = title.strip()
        for g in session.scalars(select(Game)):
            if g.title.casefold() == title.casefold():
                return GameRef(
                    id=g.id, title=g.title, system=g.system, identifiers=g.identifiers or {}
                )
        return GameRef(id=slugify(title), title=title)
    return None


def _apply_game(session: Session, svc: Services, recomp: Recomp, recipe: Recipe) -> None:
    """Point the recomp at its recipe's game; move its files if that changed."""
    if sync_recomp_game(session, recomp, recipe):
        session.commit()
        if any(a.state == ArtifactState.downloaded for v in recomp.versions for a in v.artifacts):
            svc.jobs.enqueue(session, RELOCATE, recomp_id=recomp.id)
        if recomp.game_id:
            svc.events.publish(events.GAME_UPDATED, {"id": recomp.game_id})


# ---------------------------------------------------------------- recomps


@router.get("/recomps", response_model=list[RecompSummary], summary="List recomps")
def list_recomps(
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> list[RecompSummary]:
    out = []
    for g in session.scalars(select(Recomp)):
        recipe, err = _load_recipe(svc, g.id)
        out.append(RecompSummary(**_summary(g, recipe, err, svc)))
    return sorted(out, key=lambda g: g.name.lower())


@router.post(
    "/recomps",
    response_model=RecompDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Add a recomp",
    description="From a built-in recipe (by id, or automatically when the repo has one), or "
    "from any GitHub repo URL. A repo without a built-in recipe becomes a **draft**: asset rules "
    "and the game are guessed from the repo; check them, then PATCH status=active.",
)
async def create_recomp(
    body: RecompCreate,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> RecompDetail:
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
            description = (info.get("description") or "")[:500]
            recipe = Recipe(
                id=slugify(name),
                name=_pretty_name(name),
                description=description,
                game=_game_ref(session, None, guess_game_title(name, description)),
                source=Source(repo=full, channel=channel),
                assets=rules,
                unverified=["game", "assets", "input", "platforms", "launch", "saves"],
                notes="Draft created from the repository's releases. Check the asset rules.",
            )

    for g in session.scalars(select(Recomp)):
        other, _ = _load_recipe(svc, g.id)
        if other and other.source.repo.lower() == recipe.source.repo.lower():
            raise ApiError(409, "conflict", f"This repo is already in the library as {g.id!r}")
    base, n = recipe.id, 2
    while session.get(Recomp, recipe.id) is not None or svc.recipes.exists(recipe.id):
        recipe = recipe.model_copy(update={"id": f"{base}-{n}"})
        n += 1

    if body.game_id:
        recipe = recipe.model_copy(update={"game": _game_ref(session, body.game_id, None)})
    is_draft = releases is not None
    recomp = Recomp(id=recipe.id, status=RecompStatus.draft if is_draft else RecompStatus.active)
    builtin = svc.recipes.builtin_for_repo(recipe.source.repo)
    if builtin is not None and not body.game_id:
        reset_to_builtin(svc.recipes, recomp, builtin)
    else:
        recomp.recipe_hash = svc.recipes.save(recipe)
    session.add(recomp)
    sync_recomp_game(session, recomp, recipe)
    if releases is not None:
        upsert_versions(session, recomp, releases)
        newest = newest_on_channel(releases, recipe.source.channel)
        recomp.latest_version = newest["tag"] if newest else None
    session.commit()
    if not is_draft:
        # Fetch releases now, but let the user start the first download.
        svc.jobs.enqueue(session, CHECK, recomp_id=recomp.id, params={"auto_download": False})
    svc.events.publish(events.RECOMP_UPDATED, {"id": recomp.id})
    if recomp.game_id:
        svc.events.publish(events.GAME_UPDATED, {"id": recomp.game_id})
    session.refresh(recomp)
    return _detail(recomp, svc)


@router.get("/recomps/{recomp_id}", response_model=RecompDetail, summary="Get a recomp")
def get_recomp(
    recomp_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> RecompDetail:
    return _detail(_recomp(session, recomp_id), svc)


@router.patch("/recomps/{recomp_id}", response_model=RecompDetail, summary="Change a recomp")
def update_recomp(
    recomp_id: str,
    body: RecompPatch,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> RecompDetail:
    recomp = _recomp(session, recomp_id)
    recipe, err = _load_recipe(svc, recomp_id)
    if body.recipe is not None:
        if body.recipe.id != recomp_id:
            raise ApiError(422, "validation_error", "recipe.id must match the recomp id")
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
    if body.game_id or body.game_title:
        ref = _game_ref(session, body.game_id, body.game_title)
        data["game"] = ref.model_dump() if ref else None
        if "game" in data["unverified"]:
            data["unverified"] = [u for u in data["unverified"] if u != "game"]
    channel_changed = data["source"]["channel"] != recipe.source.channel
    try:
        updated = Recipe.model_validate(data)
    except ValidationError as e:
        details = [
            {"loc": ["body", *e_["loc"]], "msg": e_["msg"], "type": e_["type"]} for e_ in e.errors()
        ]
        raise ApiError(422, "validation_error", "The recipe isn't valid", details) from e
    recomp.recipe_hash = svc.recipes.save(updated)
    recomp.builtin_hash = None  # edited: built-in updates no longer apply by themselves
    _apply_game(session, svc, recomp, updated)

    confirm = body.status == "active" and recomp.status == RecompStatus.draft
    if confirm:
        recomp.status = RecompStatus.active
    session.commit()
    if confirm or channel_changed:
        svc.jobs.enqueue(session, CHECK, recomp_id=recomp.id, params={"auto_download": False})
    svc.events.publish(events.RECOMP_UPDATED, {"id": recomp.id})
    session.refresh(recomp)
    return _detail(recomp, svc)


@router.delete(
    "/recomps/{recomp_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a recomp"
)
def delete_recomp(
    recomp_id: str,
    delete_files: bool = Query(default=True, description="Also delete downloaded files"),
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> None:
    recomp = _recomp(session, recomp_id)
    busy = session.scalar(
        select(Job.id).where(
            Job.recomp_id == recomp_id, Job.status.in_([JobStatus.queued, JobStatus.running])
        )
    )
    if busy is not None:
        raise ApiError(
            409, "busy", "A job for this recomp is still queued or running. Cancel it first."
        )
    if delete_files:
        folder = storage.resolve(
            svc.env.data_dir, storage.recomp_dir_rel(recomp.game_id, recomp_id)
        )
        storage.remove_tree(folder)
        storage.remove_empty_dirs(folder.parent, storage.resolve(svc.env.data_dir, "library"))
    game_id = recomp.game_id
    session.delete(recomp)
    session.commit()
    svc.recipes.delete(recomp_id)
    svc.events.publish(events.RECOMP_UPDATED, {"id": recomp_id})
    if game_id:
        svc.events.publish(events.GAME_UPDATED, {"id": game_id})


@router.post(
    "/recomps/{recomp_id}/check",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Check for new releases",
)
def check_recomp(
    recomp_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    _recomp(session, recomp_id)
    return job_summary(
        svc.jobs.enqueue(session, CHECK, recomp_id=recomp_id, params={"auto_download": False})
    )


@router.post(
    "/recomps/{recomp_id}/update",
    response_model=JobSummary,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Download a version (default: the newest)",
)
def download_recomp(
    recomp_id: str,
    body: UpdateRequest | None = None,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> JobSummary:
    recomp = _recomp(session, recomp_id)
    if recomp.status == RecompStatus.draft:
        raise ApiError(409, "draft", "Confirm the draft recipe before downloading")
    body = body or UpdateRequest()
    if body.version:
        _version(session, recomp_id, body.version)
    elif not recomp.latest_version:
        raise ApiError(409, "no_version", "No release known yet. Check for updates first.")
    params = {"version": body.version or recomp.latest_version}
    if body.make_current is not None:
        params["make_current"] = body.make_current
    return job_summary(svc.jobs.enqueue(session, DOWNLOAD, recomp_id=recomp_id, params=params))


# ---------------------------------------------------------------- versions


@router.get(
    "/recomps/{recomp_id}/versions",
    response_model=list[VersionOut],
    summary="List versions, newest first",
    tags=["versions"],
)
def list_versions(
    recomp_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> list[VersionOut]:
    recomp = _recomp(session, recomp_id)
    recipe, _ = _load_recipe(svc, recomp_id)
    vs = sorted(
        recomp.versions, key=lambda v: (v.published_at is not None, v.published_at), reverse=True
    )
    return [_version_out(recomp, v, recipe) for v in vs]


@router.get(
    "/recomps/{recomp_id}/versions/{version}",
    response_model=VersionOut,
    summary="Get a version",
    tags=["versions"],
)
def get_version(
    recomp_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> VersionOut:
    recomp = _recomp(session, recomp_id)
    recipe, _ = _load_recipe(svc, recomp_id)
    return _version_out(recomp, _version(session, recomp_id, version), recipe)


def _set_pin(session: Session, svc: Services, recomp_id: str, tag: str, pinned: bool) -> VersionOut:
    recomp = _recomp(session, recomp_id)
    v = _version(session, recomp_id, tag)
    v.pinned = pinned
    session.commit()
    recipe, _ = _load_recipe(svc, recomp_id)
    return _version_out(recomp, v, recipe)


@router.post(
    "/recomps/{recomp_id}/versions/{version}/pin",
    response_model=VersionOut,
    summary="Pin a version (never pruned)",
    tags=["versions"],
)
def pin_version(
    recomp_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    return _set_pin(session, svc, recomp_id, version, True)


@router.delete(
    "/recomps/{recomp_id}/versions/{version}/pin",
    response_model=VersionOut,
    summary="Unpin a version",
    tags=["versions"],
)
def unpin_version(
    recomp_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    return _set_pin(session, svc, recomp_id, version, False)


@router.post(
    "/recomps/{recomp_id}/versions/{version}/activate",
    response_model=VersionOut,
    summary="Make this the current version (rollback or roll forward)",
    tags=["versions"],
)
def activate_version(
    recomp_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    recomp = _recomp(session, recomp_id)
    v = _version(session, recomp_id, version)
    if v.state != VersionState.downloaded:
        raise ApiError(409, "not_downloaded", "Download this version before making it current")
    recomp.current_version = v.tag
    session.commit()
    svc.events.publish(events.RECOMP_UPDATED, {"id": recomp_id})
    recipe, _ = _load_recipe(svc, recomp_id)
    return _version_out(recomp, v, recipe)


@router.delete(
    "/recomps/{recomp_id}/versions/{version}",
    response_model=VersionOut,
    summary="Delete a version's downloaded files",
    description="The version stays listed and can be downloaded again.",
    tags=["versions"],
)
def delete_version(
    recomp_id: str,
    version: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> VersionOut:
    recomp = _recomp(session, recomp_id)
    v = _version(session, recomp_id, version)
    if v.tag == recomp.current_version:
        raise ApiError(
            409, "current", "This is the current version. Make another one current first."
        )
    if v.pinned:
        raise ApiError(409, "pinned", "Unpin this version before deleting its files")
    if v.state == VersionState.downloading:
        raise ApiError(409, "busy", "This version is downloading")
    delete_version_files(session, svc.env.data_dir, v)
    session.commit()
    recipe, _ = _load_recipe(svc, recomp_id)
    return _version_out(recomp, v, recipe)


# ---------------------------------------------------------------- artifacts


def _artifact_out(a: Artifact) -> ArtifactOut:
    return ArtifactOut(
        id=a.id,
        name=a.name,
        platform=a.platform,
        size=a.size,
        sha256=a.sha256,
        state=a.state,
        downloaded_at=a.downloaded_at,
    )


@router.get(
    "/recomps/{recomp_id}/versions/{version}/artifacts",
    response_model=list[ArtifactOut],
    summary="List a version's stored files",
    tags=["artifacts"],
)
def list_artifacts(
    recomp_id: str,
    version: str,
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_token),
) -> list[ArtifactOut]:
    v = _version(session, recomp_id, version)
    return [_artifact_out(a) for a in sorted(v.artifacts, key=lambda a: (a.platform, a.name))]


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


@router.post(
    "/recomps/{recomp_id}/reset-recipe",
    response_model=RecompDetail,
    summary="Replace the recipe with the current built-in one",
    description="Undoes edits. Unedited recipes are refreshed automatically at startup.",
)
def reset_recipe(
    recomp_id: str,
    session: Session = Depends(get_session),
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_admin),
) -> RecompDetail:
    recomp = _recomp(session, recomp_id)
    builtin, _differs = builtin_status(svc.recipes, recomp)
    if builtin is None:
        raise ApiError(409, "no_builtin", "There's no built-in recipe for this repo")
    recipe = reset_to_builtin(svc.recipes, recomp, builtin)
    _apply_game(session, svc, recomp, recipe)
    session.commit()
    svc.events.publish(events.RECOMP_UPDATED, {"id": recomp.id})
    session.refresh(recomp)
    return _detail(recomp, svc)
