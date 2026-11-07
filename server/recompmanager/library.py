"""Library operations: checking for releases, downloading versions, pruning, and
keeping files where the layout says they belong.

Job handlers here: ``check``, ``download``, ``relocate``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import app_settings, events, storage
from recompmanager.github import SourceError, newest_on_channel
from recompmanager.jobs import JobContext, JobError, job_handler
from recompmanager.models import (
    Artifact,
    ArtifactState,
    Game,
    Recomp,
    RecompStatus,
    Version,
    VersionState,
    utcnow,
)
from recompmanager.recipes.matching import match_assets
from recompmanager.recipes.schema import GameRef, Recipe
from recompmanager.recipes.store import RecipeError, RecipeStore

log = logging.getLogger(__name__)

CHECK = "check"
DOWNLOAD = "download"
RELOCATE = "relocate"
DISK_MARGIN = 1024**3  # keep 1 GiB free beyond what a download needs


# ---------------------------------------------------------------- games


def ensure_game(session: Session, ref: GameRef) -> Game:
    """Create the game a recipe points at, or fill in what it's missing.

    Once a game exists, its title is edited through the games API, not recipes.
    """
    game = session.get(Game, ref.id)
    if game is None:
        game = Game(id=ref.id, title=ref.title, system=ref.system, identifiers=ref.identifiers)
        session.add(game)
    else:
        if ref.system and not game.system:
            game.system = ref.system
        if ref.identifiers:
            game.identifiers = {**ref.identifiers, **(game.identifiers or {})}
    session.flush()
    return game


def sync_recomp_game(session: Session, recomp: Recomp, recipe: Recipe) -> bool:
    """Point the recomp at its recipe's game. Returns True when the game changed."""
    new_id = ensure_game(session, recipe.game).id if recipe.game else None
    if recomp.game_id != new_id:
        recomp.game_id = new_id
        return True
    return False


def layout_ok(session: Session, recomp: Recomp) -> bool:
    for v in recomp.versions:
        for a in v.artifacts:
            if a.state == ArtifactState.downloaded and a.rel_path != expected_rel(recomp, v, a):
                return False
    return True


def builtin_status(recipes: RecipeStore, recomp: Recomp) -> tuple[Recipe | None, bool]:
    """(built-in recipe for this recomp's repo, whether the library copy differs from it)."""
    try:
        current = recipes.load(recomp.id)
    except RecipeError:
        return None, False
    builtin = recipes.builtin_for_repo(current.source.repo)
    if builtin is None:
        return None, False
    _r, h = recipes.builtin_as(builtin, recomp.id)
    return builtin, h != recipes.file_hash(recomp.id)


def reset_to_builtin(recipes: RecipeStore, recomp: Recomp, builtin: Recipe) -> Recipe:
    r, h = recipes.builtin_as(builtin, recomp.id)
    recomp.recipe_hash = recipes.save(r)
    recomp.builtin_hash = h
    return r


def refresh_from_builtin(recipes: RecipeStore, recomp: Recomp) -> str | None:
    """Bring a library recipe up to date with its built-in, where that's safe.

    - Copied from a built-in and not edited since: replaced by the newer built-in.
    - Copied before this was tracked: only gains a missing game block. The API
      flags it so the user can reset it, since edits can't be told apart.
    """
    builtin, differs = builtin_status(recipes, recomp)
    if builtin is None or not differs:
        return None
    if recomp.builtin_hash and recomp.recipe_hash == recipes.file_hash(recomp.id):
        reset_to_builtin(recipes, recomp, builtin)
        return "updated to the newer built-in recipe"
    if recomp.builtin_hash is None and recomp.recipe_hash is None:
        current = recipes.load(recomp.id)
        if current.game is None and builtin.game is not None:
            recomp.recipe_hash = recipes.save(current.model_copy(update={"game": builtin.game}))
            return "game taken from the built-in recipe"
    return None


def reconcile_all(session: Session, recipes: RecipeStore, jobs) -> None:
    """At startup: refresh recipes from built-ins, match recomps to their games, and
    queue file moves where the layout changed."""
    for recomp in session.scalars(select(Recomp)):
        if msg := refresh_from_builtin(recipes, recomp):
            log.info("Recipe %s: %s", recomp.id, msg)
        try:
            sync_recomp_game(session, recomp, recipes.load(recomp.id))
        except RecipeError:
            continue
    session.commit()
    for recomp in session.scalars(select(Recomp)):
        if not layout_ok(session, recomp):
            jobs.enqueue(session, RELOCATE, recomp_id=recomp.id)


# ---------------------------------------------------------------- versions


def upsert_versions(session: Session, recomp: Recomp, releases: list[dict[str, Any]]) -> list[str]:
    """Record releases as versions. Returns tags that are new."""
    existing = {v.tag: v for v in recomp.versions}
    new: list[str] = []
    for r in releases:
        v = existing.get(r["tag"])
        if v is None:
            v = Version(
                recomp_id=recomp.id, tag=r["tag"], state=VersionState.available, pinned=False
            )
            session.add(v)
            recomp.versions.append(v)
            new.append(r["tag"])
        v.name = r["name"]
        v.published_at = r["published_at"]
        v.prerelease = r["prerelease"]
        v.notes = r["notes"]
        v.url = r["url"]
        v.assets = r["assets"]
    return new


def version_key(v: Version) -> tuple:
    return (v.published_at is not None, v.published_at, v.id or 0)


def update_available(recomp: Recomp) -> bool:
    if not recomp.latest_version:
        return False
    if recomp.current_version is None:
        return True
    if recomp.latest_version == recomp.current_version:
        return False
    by_tag = {v.tag: v for v in recomp.versions}
    latest, current = by_tag.get(recomp.latest_version), by_tag.get(recomp.current_version)
    if latest is None or current is None:
        return True
    return version_key(latest) > version_key(current)


def expected_rel(recomp: Recomp, version: Version, artifact: Artifact) -> str:
    return storage.artifact_rel(
        recomp.game_id, recomp.id, artifact.platform, version.tag, artifact.name
    )


def _recipe(ctx: JobContext, recomp_id: str) -> Recipe:
    try:
        return ctx.services.recipes.load(recomp_id)
    except RecipeError as e:
        raise JobError(f"Recipe problem: {e}") from e


# ---------------------------------------------------------------- check


@job_handler(CHECK)
async def check_recomp(ctx: JobContext) -> dict[str, Any]:
    recomp_id = ctx.recomp_id
    assert recomp_id is not None
    recipe = _recipe(ctx, recomp_id)
    ctx.progress(None, f"Checking {recipe.source.repo}")
    ctx.log(f"Fetching releases of {recipe.source.repo}")
    try:
        releases = await ctx.services.github.releases(recipe.source.repo)
    except SourceError as e:
        with ctx.session() as s:
            recomp = s.get(Recomp, recomp_id)
            if recomp:
                recomp.last_checked_at = utcnow()
                recomp.last_check_error = str(e)
        raise JobError(str(e)) from e

    newest = newest_on_channel(releases, recipe.source.channel)
    with ctx.session() as s:
        recomp = s.get(Recomp, recomp_id)
        if recomp is None:
            raise JobError("The recomp was removed")
        new_tags = upsert_versions(s, recomp, releases)
        recomp.latest_version = newest["tag"] if newest else None
        recomp.last_checked_at = utcnow()
        recomp.last_check_error = None
        s.flush()
        available = update_available(recomp)
        auto = (
            available
            and recomp.status == RecompStatus.active
            and recipe.source.auto_update
            and ctx.params.get("auto_download", True)
        )
        latest = recomp.latest_version
    ctx.log(
        f"{len(releases)} releases, {len(new_tags)} new. Newest on the {recipe.source.channel} "
        f"channel: {latest or 'none'}"
    )
    if newest is None:
        ctx.log("No release on this channel. Pre-release only projects need channel: prerelease.")
    ctx.services.events.publish(events.RECOMP_UPDATED, {"id": recomp_id})
    if available:
        ctx.services.events.publish(events.UPDATE_AVAILABLE, {"id": recomp_id, "version": latest})
    if auto:
        with ctx.session() as s:
            job = ctx.manager.enqueue(s, DOWNLOAD, recomp_id=recomp_id, params={"version": latest})
            ctx.log(f"Auto-update is on: queued download job {job.id}")
    ctx.progress(
        1.0,
        f"{len(new_tags)} new; update available: {latest}"
        if available
        else f"Up to date ({latest or 'no releases'})",
    )
    return {"new_versions": new_tags, "latest": latest, "update_available": available}


# ---------------------------------------------------------------- download


@dataclass(frozen=True)
class _File:
    """One upstream asset and the artifact rows (one per platform) it fills."""

    name: str
    size: int
    digest: str | None
    url: str
    artifact_ids: tuple[int, ...]
    rel_paths: tuple[str, ...]
    done: bool


@job_handler(DOWNLOAD)
async def download_version(ctx: JobContext) -> dict[str, Any]:
    """Download one version's assets. Params: version (tag; default latest), make_current."""
    recomp_id = ctx.recomp_id
    assert recomp_id is not None
    recipe = _recipe(ctx, recomp_id)
    env = ctx.services.env
    with ctx.session() as s:
        recomp = s.get(Recomp, recomp_id)
        if recomp is None:
            raise JobError("The recomp was removed")
        tag = ctx.params.get("version") or recomp.latest_version
        if not tag:
            raise JobError("No version to download. Check for updates first.")
        version = s.scalar(
            select(Version).where(Version.recomp_id == recomp_id, Version.tag == tag)
        )
        if version is None:
            raise JobError(f"Unknown version {tag}. Check for updates first.")
        matched = match_assets(recipe.assets, version.assets)  # platform → asset name
        if not matched:
            raise JobError(
                f"No asset in {tag} matches the recipe's asset rules. Edit the rules first."
            )
        by_name = {a["name"]: a for a in version.assets}
        existing = {(a.platform, a.name): a for a in version.artifacts}
        per_asset: dict[str, list[Artifact]] = defaultdict(list)
        for platform, name in sorted(matched.items()):
            a = by_name[name]
            art = existing.get((platform, name))
            if art is None:
                art = Artifact(
                    version_id=version.id, platform=platform, name=name, state=ArtifactState.pending
                )
                s.add(art)
                version.artifacts.append(art)
            art.size = int(a["size"] or 0)
            art.upstream_digest = a.get("digest")
            art.source_url = a["url"]
            art.rel_path = expected_rel(recomp, version, art)
            if art.state == ArtifactState.deleted:
                art.state = ArtifactState.pending
            per_asset[name].append(art)
        s.flush()
        files = [
            _File(
                name=name,
                size=arts[0].size,
                digest=arts[0].upstream_digest,
                url=arts[0].source_url,
                artifact_ids=tuple(a.id for a in arts),
                rel_paths=tuple(a.rel_path for a in arts),
                done=all(a.state == ArtifactState.downloaded for a in arts),
            )
            for name, arts in per_asset.items()
        ]
        version.state = VersionState.downloading
        version_id = version.id

    need = sum(f.size for f in files if not f.done)
    free = storage.free_bytes(env.data_dir)
    if need + DISK_MARGIN > free:
        _set_version_state(ctx, version_id, VersionState.failed)
        raise JobError(
            f"Not enough disk space: {need / 1024**3:.1f} GiB needed, "
            f"{free / 1024**3:.1f} GiB free on /data"
        )

    total = sum(f.size for f in files) or 1
    done_before = 0
    try:
        for f in files:
            paths = [storage.resolve(env.data_dir, r) for r in f.rel_paths]
            if f.done and all(p.exists() and p.stat().st_size == f.size for p in paths):
                ctx.log(f"{f.name}: already downloaded")
                done_before += f.size
                continue
            expected = None
            if f.digest and f.digest.startswith("sha256:"):
                expected = f.digest.split(":", 1)[1]
            ctx.log(f"Downloading {f.name} ({f.size / 1024**2:.1f} MiB)")
            base = done_before

            def on_progress(n: int, _total: int | None, base=base, name=f.name) -> None:
                ctx.progress((base + n) / total, f"Downloading {name}")

            try:
                got = await storage.download(
                    ctx.services.http,
                    f.url,
                    paths[0],
                    size=f.size or None,
                    sha256=expected,
                    on_progress=on_progress,
                )
            except storage.DownloadError as e:
                with ctx.session() as s:
                    for aid in f.artifact_ids:
                        if a := s.get(Artifact, aid):
                            a.state = ArtifactState.failed
                raise JobError(f"{f.name}: {e}") from e
            for extra in paths[1:]:  # same file for another platform
                storage.link_or_copy(paths[0], extra)
            with ctx.session() as s:
                for aid in f.artifact_ids:
                    if a := s.get(Artifact, aid):
                        a.sha256 = got
                        a.state = ArtifactState.downloaded
                        a.downloaded_at = utcnow()
            ctx.log(
                f"{f.name}: verified sha256 {got[:16]}..."
                if expected
                else f"{f.name}: sha256 {got[:16]}... (upstream gave no checksum to compare)"
            )
            done_before += f.size
    except BaseException:
        _set_version_state(ctx, version_id, VersionState.failed)
        raise

    make_current = ctx.params.get("make_current")
    with ctx.session() as s:
        version = s.get(Version, version_id)
        recomp = s.get(Recomp, recomp_id)
        assert version is not None and recomp is not None
        version.state = VersionState.downloaded
        version.downloaded_at = utcnow()
        if make_current is None:
            current = next((v for v in recomp.versions if v.tag == recomp.current_version), None)
            make_current = current is None or version_key(version) > version_key(current)
        if make_current:
            recomp.current_version = version.tag
            ctx.log(f"{version.tag} is now the current version")
        keep = app_settings.load(s).keep_versions
        pruned = prune(s, env.data_dir, recomp, keep)
    for t in pruned:
        ctx.log(f"Pruned {t} (keeping {keep} versions)")
    ctx.services.events.publish(events.RECOMP_UPDATED, {"id": recomp_id})
    ctx.progress(1.0, f"Downloaded {tag}")
    return {"version": tag, "files": len(files), "pruned": pruned}


def _set_version_state(ctx: JobContext, version_id: int, state: VersionState) -> None:
    with ctx.session() as s:
        v = s.get(Version, version_id)
        if v is not None and v.state == VersionState.downloading:
            v.state = state


# ---------------------------------------------------------------- deleting and pruning


def delete_version_files(session: Session, data_dir: Path, version: Version) -> None:
    library = storage.resolve(data_dir, "library")
    for a in version.artifacts:
        if a.state == ArtifactState.deleted:
            continue
        path = storage.resolve(data_dir, a.rel_path)
        path.unlink(missing_ok=True)
        storage.resolve(data_dir, a.rel_path + ".part").unlink(missing_ok=True)
        storage.remove_empty_dirs(path.parent, library)
        a.state = ArtifactState.deleted
    version.state = VersionState.pruned


def prune(session: Session, data_dir: Path, recomp: Recomp, keep: int) -> list[str]:
    """Delete files of downloaded versions beyond the newest ``keep``.

    Never touches pinned versions or the current version.
    """
    downloaded = sorted(
        (v for v in recomp.versions if v.state == VersionState.downloaded),
        key=version_key,
        reverse=True,
    )
    pruned = []
    for v in downloaded[keep:]:
        if v.pinned or v.tag == recomp.current_version:
            continue
        delete_version_files(session, data_dir, v)
        pruned.append(v.tag)
    return pruned


# ---------------------------------------------------------------- relocate


@job_handler(RELOCATE)
async def relocate(ctx: JobContext) -> dict[str, Any]:
    """Move a recomp's files to where the layout says they belong.

    Runs after the recomp's game changes, and once after upgrading from the
    older layout. Rows that shared one old file get hardlinks (or copies).
    """
    recomp_id = ctx.recomp_id
    assert recomp_id is not None
    data_dir = ctx.services.env.data_dir
    library = storage.resolve(data_dir, "library")
    moved = linked = missing = 0
    with ctx.session() as s:
        recomp = s.get(Recomp, recomp_id)
        if recomp is None:
            raise JobError("The recomp was removed")
        groups: dict[str, list[tuple[Version, Artifact]]] = defaultdict(list)
        for v in recomp.versions:
            for a in v.artifacts:
                if a.state == ArtifactState.downloaded:
                    groups[a.rel_path].append((v, a))
        total = sum(len(g) for g in groups.values()) or 1
        n = 0
        for old_rel, rows in groups.items():
            old = storage.resolve(data_dir, old_rel)
            placed: Path | None = None
            for v, a in rows:
                n += 1
                new_rel = expected_rel(recomp, v, a)
                new = storage.resolve(data_dir, new_rel)
                if new_rel == old_rel:
                    placed = new
                    continue
                if placed is None and old.exists():
                    storage.move(old, new)
                    moved += 1
                    placed = new
                elif placed is not None:
                    storage.link_or_copy(placed, new)
                    linked += 1
                elif not new.exists():
                    a.state = ArtifactState.deleted
                    missing += 1
                    continue
                a.rel_path = new_rel
                ctx.progress(n / total, f"Moving {a.name}")
            if placed is not None and placed != old and old.exists():
                old.unlink()  # every row now has its own copy or link
            storage.remove_empty_dirs(old.parent, library)
    ctx.log(f"Moved {moved} files, linked {linked}, {missing} missing")
    ctx.services.events.publish(events.RECOMP_UPDATED, {"id": recomp_id})
    ctx.progress(1.0, f"Files in place ({moved} moved)")
    return {"moved": moved, "linked": linked, "missing": missing}
