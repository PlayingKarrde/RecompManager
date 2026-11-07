"""Server-side builds and extractions in sibling containers.

A recipe's ``BuildSpec`` says which builder image to use, what to run, which release
file and inputs to mount and which outputs to keep. The server:

1. checks Docker is reachable and the host has the memory and disk the recipe asks for,
2. builds the builder image from its Dockerfile (cached by content hash),
3. runs it as a sibling container (through the host's Docker daemon) with the release
   file and inputs mounted read-only, a work folder and an output folder, resource
   limits, no network unless the recipe needs it, as the server's own user,
4. streams the container's output into the job log and a log file,
5. stores the outputs as artifacts that install plans hand to clients.

Bind mounts are resolved by the host's daemon, so paths must be host paths. The
server works them out from its own container's mounts (``docker inspect`` of
itself), falling back to ``HOST_DATA_PATH``; outside a container they're used as-is.

Builds start by themselves (``queue_builds``) when a version is downloaded or an
input becomes ready, if the Docker socket is mounted and ``auto_build`` is on.

Job handler: ``build``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import logging
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import app_settings, events, formats, storage
from recompmanager.config import Env
from recompmanager.inputs import Stopped, _in_thread, input_path, slots
from recompmanager.jobs import JobContext, JobError, job_handler
from recompmanager.models import (
    Artifact,
    ArtifactState,
    Build,
    BuildKind,
    JobStatus,
    Recomp,
    RecompStatus,
    Version,
    VersionState,
    utcnow,
)
from recompmanager.recipes.schema import BuildSpec, Recipe, Strategy
from recompmanager.recipes.store import RecipeError

log = logging.getLogger(__name__)

BUILD = "build"
BUILTIN_BUILDERS = Path(__file__).parent / "recipes" / "builtin" / "builders"
ANY = "any"  # platform of an extraction's outputs: they work everywhere
LOG_TAIL = 40


class BuildError(Exception):
    """A user-facing reason a build can't run or failed."""


@dataclass(frozen=True)
class Mount:
    source: Path  # path inside this server's container (or host, when not containerised)
    target: str  # path inside the build container
    read_only: bool = True


# ---------------------------------------------------------------- backends


def map_host_path(
    path: str,
    mounts: dict[str, str],
    data_dir: str,
    host_data_path: str | None,
    *,
    in_container: bool,
) -> str:
    """Where ``path`` (inside this server's container) is on the Docker host.

    Uses this container's own mounts first (longest match), then HOST_DATA_PATH
    for the data dir. Outside a container, paths are already host paths.
    """
    p = path.replace("\\", "/")
    best = max((d for d in mounts if p == d or p.startswith(d + "/")), key=len, default=None)
    if best is not None:
        return mounts[best] + p[len(best) :]
    data = data_dir.replace("\\", "/").rstrip("/")
    if host_data_path and (p == data or p.startswith(data + "/")):
        return host_data_path.rstrip("/") + p[len(data) :]
    if not in_container:
        return p
    raise BuildError(
        f"Can't tell where {p} is on the host. Set HOST_DATA_PATH (and mount /roms and "
        "/import as normal volumes) so build containers can see the files."
    )


class Backend(Protocol):
    def unavailable(self) -> str | None: ...

    def host_path(self, path: Path) -> str: ...

    def ensure_image(self, context: Path, tag: str, on_line: Callable[[str], None]) -> None: ...

    def run(
        self,
        image: str,
        command: list[str],
        mounts: list[Mount],
        environment: dict[str, str],
        *,
        user: str | None,
        network: bool,
        memory_gb: int | None,
        cpus: float | None,
        labels: dict[str, str],
        on_line: Callable[[str], None],
        stop: threading.Event,
        timeout_s: float,
    ) -> int: ...


class DockerBackend:
    """Talks to the host's Docker daemon through the mounted socket."""

    def __init__(self, env: Env) -> None:
        self.env = env
        self._client: Any = None
        self._mounts: dict[str, str] | None = None

    def client(self) -> Any:
        if self._client is None:
            import docker

            self._client = docker.DockerClient(
                base_url=f"unix://{self.env.docker_socket}", timeout=60
            )
        return self._client

    def unavailable(self) -> str | None:
        if not self.env.docker_socket.exists():
            return f"the Docker socket isn't mounted at {self.env.docker_socket}"
        try:
            self.client().ping()
        except Exception as e:  # docker raises many kinds
            return f"Docker isn't answering ({e.__class__.__name__})"
        return None

    def _own_mounts(self) -> dict[str, str]:
        """Container path → host path of this server's own mounts."""
        if self._mounts is None:
            self._mounts = {}
            if Path("/.dockerenv").exists():
                try:
                    me = self.client().containers.get(socket.gethostname())
                    for m in me.attrs.get("Mounts", []):
                        if m.get("Destination") and m.get("Source"):
                            self._mounts[m["Destination"].rstrip("/")] = m["Source"]
                except Exception as e:
                    log.warning("Couldn't inspect this container's mounts: %s", e)
        return self._mounts

    def host_path(self, path: Path) -> str:
        return map_host_path(
            str(path.resolve()),
            self._own_mounts(),
            str(self.env.data_dir.resolve()),
            self.env.host_data_path,
            in_container=Path("/.dockerenv").exists(),
        )

    def ensure_image(self, context: Path, tag: str, on_line: Callable[[str], None]) -> None:
        import docker

        c = self.client()
        try:
            c.images.get(tag)
            return
        except docker.errors.ImageNotFound:
            pass
        on_line(f"Building the builder image {tag} (once per builder change)")
        for chunk in c.api.build(path=str(context), tag=tag, rm=True, forcerm=True, decode=True):
            if "stream" in chunk and chunk["stream"].strip():
                on_line(chunk["stream"].rstrip())
            if "error" in chunk:
                raise BuildError(f"The builder image failed to build: {chunk['error'].strip()}")

    def run(
        self,
        image: str,
        command: list[str],
        mounts: list[Mount],
        environment: dict[str, str],
        *,
        user: str | None,
        network: bool,
        memory_gb: int | None,
        cpus: float | None,
        labels: dict[str, str],
        on_line: Callable[[str], None],
        stop: threading.Event,
        timeout_s: float,
    ) -> int:
        import docker
        import requests

        c = self.client()
        container = c.containers.run(
            image,
            command,
            detach=True,
            mounts=[
                docker.types.Mount(
                    m.target, self.host_path(m.source), type="bind", read_only=m.read_only
                )
                for m in mounts
            ],
            environment=environment,
            user=user,
            network_mode="bridge" if network else "none",
            mem_limit=f"{memory_gb}g" if memory_gb else None,
            nano_cpus=int(cpus * 1e9) if cpus else None,
            labels=labels,
            working_dir="/work",
            security_opt=["no-new-privileges"],
        )
        reader = threading.Thread(target=self._follow, args=(container, on_line), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                if stop.is_set():
                    container.kill()
                    raise Stopped
                if time.monotonic() > deadline:
                    container.kill()
                    raise BuildError(f"The build took longer than {timeout_s / 60:.0f} minutes")
                try:
                    result = container.wait(timeout=2)
                    reader.join(timeout=5)
                    return int(result.get("StatusCode", 1))
                except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
                    continue
        finally:
            with contextlib.suppress(Exception):
                container.remove(force=True)

    @staticmethod
    def _follow(container: Any, on_line: Callable[[str], None]) -> None:
        buf = b""
        with contextlib.suppress(Exception):
            for chunk in container.logs(stream=True, follow=True):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    on_line(line.decode("utf-8", "replace").rstrip("\r"))
        if buf:
            on_line(buf.decode("utf-8", "replace"))


# ---------------------------------------------------------------- what to build


@dataclass(frozen=True)
class Target:
    kind: BuildKind
    platform: str  # ANY for an extraction
    spec: BuildSpec


def targets(recipe: Recipe) -> list[Target]:
    out = []
    uses_extract = any(p.strategy == Strategy.server_extract for p in recipe.platforms.values())
    if recipe.extract and uses_extract:
        out.append(Target(BuildKind.extract, ANY, recipe.extract))
    for key, plan in recipe.platforms.items():
        if key != "default" and plan.strategy == Strategy.server_build and plan.build:
            out.append(Target(BuildKind.build, key, plan.build))
    return out


def target_for(recipe: Recipe, kind: BuildKind, platform: str) -> Target | None:
    return next((t for t in targets(recipe) if t.kind == kind and t.platform == platform), None)


def find_builder(env: Env, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise BuildError(f"Bad builder name {name!r}")
    for root in (env.recipes_dir / "builders", BUILTIN_BUILDERS):
        d = root / name
        if (d / "Dockerfile").is_file():
            return d
    raise BuildError(f"No builder called {name!r} (looked for {name}/Dockerfile)")


def context_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(folder).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def input_choice(session: Session, env: Env, recomp: Recomp, recipe: Recipe, spec: BuildSpec):
    """Slot → best ready input (verified first, never a mismatch). Raises for a missing
    required slot."""
    from recompmanager.plan import best_input

    chosen = {}
    wanted = set(spec.inputs) if spec.inputs is not None else None
    for slot in slots(recipe):
        if wanted is not None and slot.id not in wanted:
            continue
        if wanted is None and slot.optional:
            continue
        inp, _status = best_input(session, env, recomp, slot)
        if inp is None:
            if slot.optional:
                continue
            raise BuildError(f"Needs the input {slot.description or slot.id!r} on the server")
        chosen[slot.id] = inp
    return chosen


def host_memory_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024**2
    except OSError:
        return None
    return None


# ---------------------------------------------------------------- queueing


def queue_builds(
    session: Session,
    svc,
    recomp: Recomp,
    *,
    auto: bool,
    platform: str | None = None,
    version: str | None = None,
) -> list[Build]:
    """Create build jobs for the recomp's current (or given) version.

    Automatic runs skip anything already built, building, or that failed with the
    same inputs, and anything whose inputs aren't on the server yet.
    """
    if auto:
        if svc.builder.unavailable() is not None:
            return []
        if not app_settings.load(session).auto_build or recomp.status != RecompStatus.active:
            return []
    try:
        recipe = svc.recipes.load(recomp.id)
    except RecipeError:
        return []
    tag = version or recomp.current_version
    v = next((x for x in recomp.versions if x.tag == tag), None)
    if v is None or v.state != VersionState.downloaded:
        return []
    out = []
    for t in targets(recipe):
        if platform and t.platform != platform:
            continue
        try:
            chosen = input_choice(session, svc.env, recomp, recipe, t.spec)
        except BuildError:
            if auto:
                continue
            chosen = {}
        ids = sorted(i.id for i in chosen.values())
        previous = session.scalars(
            select(Build).where(
                Build.recomp_id == recomp.id,
                Build.version_tag == tag,
                Build.platform == t.platform,
            )
        ).all()
        if any(b.status in (JobStatus.queued, JobStatus.running) for b in previous):
            continue
        if auto and any(
            b.input_ids == ids and b.status in (JobStatus.succeeded, JobStatus.failed)
            for b in previous
        ):
            continue
        b = Build(
            recomp_id=recomp.id,
            version_tag=tag,
            platform=t.platform,
            kind=t.kind,
            status=JobStatus.queued,
            input_ids=ids,
            auto=auto,
        )
        session.add(b)
        session.flush()
        job = svc.jobs.enqueue(session, BUILD, recomp_id=recomp.id, params={"build_id": b.id})
        b.job_id = job.id
        session.commit()
        out.append(b)
    return out


def queue_for_game(session: Session, svc, game_id: str) -> int:
    from recompmanager.models import Game

    game = session.get(Game, game_id)
    n = 0
    for r in game.recomps if game else []:
        n += len(queue_builds(session, svc, r, auto=True))
    return n


# ---------------------------------------------------------------- the job


@job_handler(BUILD)
async def run_build(ctx: JobContext) -> dict[str, Any]:
    build_id = int(ctx.params["build_id"])
    try:
        return await _run_build(ctx, build_id)
    except asyncio.CancelledError:
        _finish(ctx, build_id, JobStatus.cancelled, "Cancelled")
        raise
    except (BuildError, JobError) as e:
        _finish(ctx, build_id, JobStatus.failed, str(e))
        raise JobError(str(e)) from e
    except Exception as e:
        _finish(ctx, build_id, JobStatus.failed, f"Unexpected error: {e}")
        raise


def _finish(ctx: JobContext, build_id: int, status: JobStatus, error: str | None) -> None:
    with ctx.session() as s:
        b = s.get(Build, build_id)
        if b is not None:
            b.status, b.error, b.finished_at = status, error, utcnow()
            ctx.services.events.publish(events.RECOMP_UPDATED, {"id": b.recomp_id})


async def _run_build(ctx: JobContext, build_id: int) -> dict[str, Any]:
    svc = ctx.services
    env: Env = svc.env
    backend: Backend = svc.builder

    with ctx.session() as s:
        b = s.get(Build, build_id)
        if b is None:
            raise BuildError("The build was removed")
        recomp = s.get(Recomp, b.recomp_id)
        assert recomp is not None
        b.status, b.started_at, b.error = JobStatus.running, utcnow(), None
        try:
            recipe = svc.recipes.load(recomp.id)
        except RecipeError as e:
            raise BuildError(f"Recipe problem: {e}") from e
        t = target_for(recipe, b.kind, b.platform)
        if t is None:
            raise BuildError(f"The recipe no longer builds {b.platform}")
        spec = t.spec
        version = s.scalar(
            select(Version).where(Version.recomp_id == recomp.id, Version.tag == b.version_tag)
        )
        if version is None or version.state != VersionState.downloaded:
            raise BuildError(f"Download {b.version_tag} on the server first")
        rel_platform = spec.release_platform or b.platform
        release = next(
            (
                a
                for a in version.artifacts
                if a.origin == "release"
                and a.platform == rel_platform
                and a.state == ArtifactState.downloaded
            ),
            None,
        )
        if release is None:
            raise BuildError(f"{b.version_tag} has no {rel_platform} release file on the server")
        chosen = input_choice(s, env, recomp, recipe, spec)
        b.input_ids = sorted(i.id for i in chosen.values())
        mounts = [
            Mount(storage.resolve(env.data_dir, release.rel_path), f"/release/{release.name}")
        ]
        environment = {
            "RELEASE": f"/release/{release.name}",
            "VERSION": b.version_tag,
            "PLATFORM": b.platform,
            "WORK": "/work",
            "OUTPUT": "/output",
            "HOME": "/work/home",
        }
        for slot, inp in chosen.items():
            src = input_path(env, inp)
            var = "INPUT_" + re.sub(r"[^A-Z0-9]", "_", slot.upper())
            if src.is_dir():
                mounts.append(Mount(src, f"/input/{slot}"))
                environment[var] = f"/input/{slot}"
            else:
                mounts.append(Mount(src, f"/input/{slot}/{src.name}"))
                environment[var] = f"/input/{slot}/{src.name}"
        base_rel = storage.build_dir_rel(recomp.game_id, recomp.id, b.platform, b.version_tag)
        b.log_rel = f"{base_rel}/build.log"
        version_id, game_id = version.id, recomp.game_id

    if reason := backend.unavailable():
        raise BuildError(f"Server builds are off: {reason}")
    base = storage.resolve(env.data_dir, base_rel)
    work, output, logfile = base / "work", base / "output", base / "build.log"
    free = storage.free_bytes(env.data_dir) / 1024**3
    if spec.min_disk_gb and free < spec.min_disk_gb:
        raise BuildError(f"Needs {spec.min_disk_gb} GB free on /data; {free:.0f} GB is free")
    mem = host_memory_gb()
    if spec.min_memory_gb and mem is not None and mem < spec.min_memory_gb:
        raise BuildError(f"Needs {spec.min_memory_gb} GB of RAM; this server has {mem:.0f} GB")

    for d in (work, output):
        storage.remove_tree(d)
        d.mkdir(parents=True, exist_ok=True)
    (work / "home").mkdir()
    context = find_builder(env, spec.builder)
    tag = f"recompmanager-builder-{spec.builder}:{context_hash(context)}"
    with ctx.session() as s:
        if bb := s.get(Build, build_id):
            bb.image = tag

    logf = logfile.open("w", encoding="utf-8", errors="replace")
    tail: list[str] = []

    def on_line(line: str) -> None:
        logf.write(line + "\n")
        logf.flush()
        tail.append(line)
        del tail[:-LOG_TAIL]
        ctx.log(line[:500])
        ctx.progress(None, line[:200] or None)

    user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else None

    def work_fn(stop: threading.Event) -> int:
        backend.ensure_image(context, tag, on_line)
        on_line(f"Running {' '.join(spec.command)} for {b.version_tag} ({b.platform})")
        return backend.run(
            tag,
            spec.command,
            [*mounts, Mount(work, "/work", False), Mount(output, "/output", False)],
            environment,
            user=user,
            network=spec.network,
            memory_gb=spec.memory_gb,
            cpus=spec.cpus,
            labels={"recompmanager.build": str(build_id), "recompmanager.recomp": recomp.id},
            on_line=on_line,
            stop=stop,
            timeout_s=spec.timeout_minutes * 60,
        )

    try:
        code = await _in_thread(ctx, work_fn)
    finally:
        logf.close()
    if code != 0:
        last = "\n".join(tail[-8:])
        raise BuildError(f"The build exited with code {code}. Last lines:\n{last}")

    found = sorted(
        {
            p
            for pattern in spec.outputs
            for p in output.rglob("*")
            if p.is_file() and fnmatch.fnmatch(p.relative_to(output).as_posix(), pattern)
        }
    )
    if not found:
        raise BuildError(f"The build finished but made none of {', '.join(spec.outputs)}")

    hashes = {}
    for p in found:
        hashes[p] = await asyncio.to_thread(formats.sha256_file, p)
    with ctx.session() as s:
        old = s.scalars(
            select(Artifact).where(
                Artifact.version_id == version_id,
                Artifact.origin == "build",
                Artifact.platform == b.platform,
            )
        ).all()
        for a in old:
            s.delete(a)
        s.flush()
        for p in found:
            s.add(
                Artifact(
                    version_id=version_id,
                    platform=b.platform,
                    name=p.relative_to(output).as_posix(),
                    size=p.stat().st_size,
                    sha256=hashes[p],
                    upstream_digest=None,
                    source_url=f"build:{build_id}",
                    rel_path=p.relative_to(env.data_dir.resolve()).as_posix(),
                    state=ArtifactState.downloaded,
                    downloaded_at=utcnow(),
                    origin="build",
                    build_id=build_id,
                )
            )
        bb = s.get(Build, build_id)
        if bb is not None:
            bb.status, bb.finished_at = JobStatus.succeeded, utcnow()
    shutil.rmtree(work, ignore_errors=True)  # can be tens of GB
    ctx.log(f"Kept {len(found)} output file(s): {', '.join(p.name for p in found)}")
    ctx.progress(1.0, f"Built {b.version_tag} for {b.platform}")
    svc.events.publish(events.RECOMP_UPDATED, {"id": recomp.id})
    if game_id:
        svc.events.publish(events.GAME_UPDATED, {"id": game_id})
    return {"build_id": build_id, "outputs": [p.name for p in found]}
