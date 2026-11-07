"""Library operations: checking for releases, downloading versions, pruning.

The ``check`` and ``download`` job handlers live here.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    GameStatus,
    Version,
    VersionState,
    utcnow,
)
from recompmanager.recipes.matching import platforms_by_asset
from recompmanager.recipes.schema import Recipe
from recompmanager.recipes.store import RecipeError

CHECK = "check"
DOWNLOAD = "download"
DISK_MARGIN = 1024**3  # keep 1 GiB free beyond what a download needs


@dataclass(frozen=True)
class _Todo:
    id: int
    name: str
    size: int
    digest: str | None
    url: str
    rel_path: str
    done: bool


def upsert_versions(session: Session, game: Game, releases: list[dict[str, Any]]) -> list[str]:
    """Record releases as versions. Returns tags that are new."""
    existing = {v.tag: v for v in game.versions}
    new: list[str] = []
    for r in releases:
        v = existing.get(r["tag"])
        if v is None:
            v = Version(game_id=game.id, tag=r["tag"], state=VersionState.available, pinned=False)
            session.add(v)
            game.versions.append(v)
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


def update_available(game: Game) -> bool:
    if not game.latest_version:
        return False
    if game.current_version is None:
        return True
    if game.latest_version == game.current_version:
        return False
    by_tag = {v.tag: v for v in game.versions}
    latest, current = by_tag.get(game.latest_version), by_tag.get(game.current_version)
    if latest is None or current is None:
        return True
    return version_key(latest) > version_key(current)


def _recipe(ctx: JobContext, game_id: str) -> Recipe:
    try:
        return ctx.services.recipes.load(game_id)
    except RecipeError as e:
        raise JobError(f"Recipe problem: {e}") from e


@job_handler(CHECK)
async def check_game(ctx: JobContext) -> dict[str, Any]:
    game_id = ctx.game_id
    assert game_id is not None
    recipe = _recipe(ctx, game_id)
    ctx.progress(None, f"Checking {recipe.source.repo}")
    ctx.log(f"Fetching releases of {recipe.source.repo}")
    try:
        releases = await ctx.services.github.releases(recipe.source.repo)
    except SourceError as e:
        with ctx.session() as s:
            game = s.get(Game, game_id)
            if game:
                game.last_checked_at = utcnow()
                game.last_check_error = str(e)
        raise JobError(str(e)) from e

    newest = newest_on_channel(releases, recipe.source.channel)
    with ctx.session() as s:
        game = s.get(Game, game_id)
        if game is None:
            raise JobError("The game was removed")
        new_tags = upsert_versions(s, game, releases)
        game.latest_version = newest["tag"] if newest else None
        game.last_checked_at = utcnow()
        game.last_check_error = None
        s.flush()
        available = update_available(game)
        auto = (
            available
            and game.status == GameStatus.active
            and recipe.source.auto_update
            and ctx.params.get("auto_download", True)
        )
        latest = game.latest_version
    ctx.log(
        f"{len(releases)} releases, {len(new_tags)} new. Newest on the {recipe.source.channel} "
        f"channel: {latest or 'none'}"
    )
    if newest is None:
        ctx.log("No release on this channel. Pre-release only projects need channel: prerelease.")
    ctx.services.events.publish(events.GAME_UPDATED, {"id": game_id})
    if available:
        ctx.services.events.publish(events.UPDATE_AVAILABLE, {"id": game_id, "version": latest})
    if auto:
        with ctx.session() as s:
            job = ctx.manager.enqueue(s, DOWNLOAD, game_id, {"version": latest})
            ctx.log(f"Auto-update is on: queued download job {job.id}")
    ctx.progress(
        1.0,
        f"{len(new_tags)} new; update available: {latest}"
        if available
        else f"Up to date ({latest or 'no releases'})",
    )
    return {"new_versions": new_tags, "latest": latest, "update_available": available}


@job_handler(DOWNLOAD)
async def download_version(ctx: JobContext) -> dict[str, Any]:
    """Download one version's assets. Params: version (tag; default latest), make_current."""
    game_id = ctx.game_id
    assert game_id is not None
    recipe = _recipe(ctx, game_id)
    env = ctx.services.env
    with ctx.session() as s:
        game = s.get(Game, game_id)
        if game is None:
            raise JobError("The game was removed")
        tag = ctx.params.get("version") or game.latest_version
        if not tag:
            raise JobError("No version to download. Check for updates first.")
        version = s.scalar(select(Version).where(Version.game_id == game_id, Version.tag == tag))
        if version is None:
            raise JobError(f"Unknown version {tag}. Check for updates first.")
        mapping = platforms_by_asset(recipe.assets, version.assets)
        if not mapping:
            raise JobError(
                f"No asset in {tag} matches the recipe's asset rules. Edit the rules for this game."
            )
        by_name = {a["name"]: a for a in version.assets}
        todo: list[_Todo] = []
        for name, plats in mapping.items():
            a = by_name[name]
            art = s.scalar(
                select(Artifact).where(Artifact.version_id == version.id, Artifact.name == name)
            )
            if art is None:
                art = Artifact(version_id=version.id, name=name, state=ArtifactState.pending)
                s.add(art)
            art.platforms = plats
            art.size = int(a["size"] or 0)
            art.upstream_digest = a.get("digest")
            art.source_url = a["url"]
            art.rel_path = storage.artifact_rel(game_id, tag, name)
            if art.state == ArtifactState.deleted:
                art.state = ArtifactState.pending
            s.flush()
            todo.append(
                _Todo(
                    art.id,
                    name,
                    art.size,
                    art.upstream_digest,
                    art.source_url,
                    art.rel_path,
                    art.state == ArtifactState.downloaded,
                )
            )
        version.state = VersionState.downloading
        version_id = version.id

    need = sum(t.size for t in todo if not t.done)
    free = storage.free_bytes(env.data_dir)
    if need + DISK_MARGIN > free:
        _set_version_state(ctx, version_id, VersionState.failed)
        raise JobError(
            f"Not enough disk space: {need / 1024**3:.1f} GiB needed, "
            f"{free / 1024**3:.1f} GiB free on /data"
        )

    total = sum(t.size for t in todo) or 1
    done_before = 0
    try:
        for t in todo:
            art_id, name, size, digest, url = t.id, t.name, t.size, t.digest, t.url
            dest = storage.resolve(env.data_dir, t.rel_path)
            if t.done and dest.exists() and dest.stat().st_size == size:
                ctx.log(f"{name}: already downloaded")
                done_before += size
                continue
            expected = None
            if digest and digest.startswith("sha256:"):
                expected = digest.split(":", 1)[1]
            ctx.log(f"Downloading {name} ({size / 1024**2:.1f} MiB)")
            base = done_before

            def on_progress(n: int, _total: int | None, base=base, name=name) -> None:
                ctx.progress((base + n) / total, f"Downloading {name}")

            try:
                got = await storage.download(
                    ctx.services.http,
                    url,
                    dest,
                    size=size or None,
                    sha256=expected,
                    on_progress=on_progress,
                )
            except storage.DownloadError as e:
                with ctx.session() as s:
                    a = s.get(Artifact, art_id)
                    if a:
                        a.state = ArtifactState.failed
                raise JobError(f"{name}: {e}") from e
            with ctx.session() as s:
                a = s.get(Artifact, art_id)
                if a:
                    a.sha256 = got
                    a.state = ArtifactState.downloaded
                    a.downloaded_at = utcnow()
            ctx.log(
                f"{name}: verified sha256 {got[:16]}…"
                if expected
                else f"{name}: sha256 {got[:16]}… (upstream gave no checksum to compare)"
            )
            done_before += size
    except BaseException:
        _set_version_state(ctx, version_id, VersionState.failed)
        raise

    make_current = ctx.params.get("make_current")
    with ctx.session() as s:
        version = s.get(Version, version_id)
        game = s.get(Game, game_id)
        assert version is not None and game is not None
        version.state = VersionState.downloaded
        version.downloaded_at = utcnow()
        if make_current is None:
            current = next((v for v in game.versions if v.tag == game.current_version), None)
            make_current = current is None or version_key(version) > version_key(current)
        if make_current:
            game.current_version = version.tag
            ctx.log(f"{version.tag} is now the current version")
        keep = app_settings.load(s).keep_versions
        pruned = prune(s, env.data_dir, game, keep)
    for t in pruned:
        ctx.log(f"Pruned {t} (keeping {keep} versions)")
    ctx.services.events.publish(events.GAME_UPDATED, {"id": game_id})
    ctx.progress(1.0, f"Downloaded {tag}")
    return {"version": tag, "artifacts": len(todo), "pruned": pruned}


def _set_version_state(ctx: JobContext, version_id: int, state: VersionState) -> None:
    with ctx.session() as s:
        v = s.get(Version, version_id)
        if v is not None and v.state == VersionState.downloading:
            v.state = state


def delete_version_files(session: Session, data_dir, version: Version) -> None:
    for a in version.artifacts:
        if a.state != ArtifactState.deleted:
            storage.resolve(data_dir, a.rel_path).unlink(missing_ok=True)
            storage.resolve(data_dir, a.rel_path + ".part").unlink(missing_ok=True)
            a.state = ArtifactState.deleted
    storage.remove_tree(
        storage.resolve(data_dir, storage.version_dir_rel(version.game_id, version.tag))
    )
    version.state = VersionState.pruned


def prune(session: Session, data_dir, game: Game, keep: int) -> list[str]:
    """Delete files of downloaded versions beyond the newest ``keep``.

    Never touches pinned versions or the current version.
    """
    downloaded = sorted(
        (v for v in game.versions if v.state == VersionState.downloaded),
        key=version_key,
        reverse=True,
    )
    pruned = []
    for v in downloaded[keep:]:
        if v.pinned or v.tag == game.current_version:
            continue
        delete_version_files(session, data_dir, v)
        pruned.append(v.tag)
    return pruned
