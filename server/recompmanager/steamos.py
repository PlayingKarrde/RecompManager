"""Can this recomp run on SteamOS, and what stands in the way?

SteamOS (Steam Deck and other SteamOS machines) runs Linux builds but has no
compilers and a read-only system, so recomps whose Linux build must be compiled
can't be set up there by hand. The server answers per recomp, from the same
install plan a ``linux-x64-steamdeck`` client would get, plus the build history
and the verdicts on the game's inputs.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager.config import Env
from recompmanager.inputs import evaluate_slot, slots
from recompmanager.models import Build, InputState, JobStatus, Recomp, VersionState
from recompmanager.plan import _pick, best_input, build_plan
from recompmanager.platforms import fallbacks
from recompmanager.recipes.schema import InputSlot, Recipe, Strategy

PLATFORM = "linux-x64-steamdeck"


class SteamOSState(StrEnum):
    ready = "ready"  # installs and runs: a Linux release as is, or the server's build
    needs_files = "needs_files"  # your game files are missing on the server, or wrong
    not_downloaded = "not_downloaded"  # the Linux release isn't on the server yet
    building = "building"  # the server is building it now
    not_built = "not_built"  # the server can build it but hasn't yet
    build_failed = "build_failed"  # the server's last build of this version failed
    builds_off = "builds_off"  # needs a server build, but builds can't run here
    needs_compiler = "needs_compiler"  # only buildable on the device, which SteamOS can't do
    no_linux = "no_linux"  # no Linux release at all
    unknown = "unknown"  # the recipe can't be read


class SteamOSVia(StrEnum):
    release = "release"  # the project's Linux release
    server_build = "server_build"  # built on this server
    device_build = "device_build"  # built on the device (a distrobox on SteamOS)


class SteamOSStatus(BaseModel):
    state: SteamOSState
    via: SteamOSVia | None = Field(description="How it gets onto SteamOS once it can")
    summary: str = Field(description="A few words for a badge")
    detail: str = Field(description="One or two sentences on what this means")
    problems: list[str] = Field(description="What to fix on the server, if anything")
    version: str | None
    build_id: int | None = Field(description="The latest server build for this version")


def _status(state, via, summary, detail, *, problems=(), version=None, build_id=None):
    return SteamOSStatus(
        state=state,
        via=via,
        summary=summary,
        detail=detail,
        problems=list(problems),
        version=version,
        build_id=build_id,
    )


def input_problems(
    session: Session, env: Env, recomp: Recomp, needed: list[InputSlot]
) -> list[str]:
    """Why each needed slot has no usable input: missing, or the wrong dump (and why)."""
    out = []
    for slot in needed:
        inp, _status = best_input(session, env, recomp, slot)
        if inp is not None:
            continue
        what = slot.description or slot.id
        wrong = []
        if recomp.game is not None:
            for i in recomp.game.inputs:
                if i.state != InputState.ready:
                    continue
                v = evaluate_slot(session, env, i, recomp.game, slot)
                if v.status == "mismatch":
                    wrong.append(f"{i.name}: {v.reason}")
        if wrong:
            out.append(f"None of your files fit ({what}). " + "; ".join(wrong))
        else:
            out.append(f"Add your game files on the server: {what}")
    return out


def _needed_slots(recipe: Recipe, inputs: list[str] | None) -> list[InputSlot]:
    all_slots = slots(recipe)
    if inputs is not None:
        return [s for s in all_slots if s.id in inputs and not s.optional]
    return [s for s in all_slots if not s.optional]


def steamos_status(
    session: Session,
    env: Env,
    builds_unavailable: str | None,
    recomp: Recomp,
    recipe: Recipe | None,
) -> SteamOSStatus:
    if recipe is None:
        return _status(
            SteamOSState.unknown, None, "Unknown", "The recipe can't be read, so this isn't known."
        )
    keys = fallbacks(PLATFORM)
    if not any(k in recipe.assets for k in keys):
        others = ", ".join(sorted(recipe.assets)) or "none"
        return _status(
            SteamOSState.no_linux,
            None,
            "No Linux build",
            "The project doesn't release a Linux build. A Windows build may still run "
            f"through Proton. Releases: {others}.",
        )

    _key, pplan = _pick(recipe.platforms, keys)
    strategy = pplan.strategy if pplan else Strategy.runtime
    tag = recomp.current_version
    version = next((v for v in recomp.versions if v.tag == tag), None) if tag else None

    if strategy == Strategy.client_build:
        notes = pplan.notes if pplan and pplan.notes else ""
        return _status(
            SteamOSState.needs_compiler,
            SteamOSVia.device_build,
            "Needs build tools",
            "It has to be compiled on the device, and SteamOS has no compiler. Build it in a "
            "distrobox, or give the recipe a server build. " + notes,
            version=tag,
        )

    if version is None or version.state != VersionState.downloaded:
        return _status(
            SteamOSState.not_downloaded,
            None,
            "Not downloaded",
            "The Linux release isn't on the server yet. Download a version first.",
            version=tag,
        )

    if strategy in (Strategy.server_build, Strategy.server_extract):
        plan = build_plan(session, env, recomp, recipe, PLATFORM)
        if plan.strategy == strategy and plan.ready:
            what = (
                "Built on the server"
                if strategy == Strategy.server_build
                else "Prepared on the server"
            )
            return _status(
                SteamOSState.ready,
                SteamOSVia.server_build,
                what,
                f"{what} for {tag}; the device just downloads it.",
                version=tag,
            )
        platforms = ["any"] if strategy == Strategy.server_extract else keys
        latest = session.scalar(
            select(Build)
            .where(
                Build.recomp_id == recomp.id,
                Build.version_tag == tag,
                Build.platform.in_(platforms),
            )
            .order_by(Build.id.desc())
        )
        build_id = latest.id if latest else None
        if latest and latest.status in (JobStatus.queued, JobStatus.running):
            return _status(
                SteamOSState.building,
                SteamOSVia.server_build,
                "Building",
                f"The server is building {tag} now.",
                version=tag,
                build_id=build_id,
            )
        spec = recipe.extract if strategy == Strategy.server_extract else (pplan and pplan.build)
        problems = input_problems(
            session, env, recomp, _needed_slots(recipe, spec.inputs if spec else None)
        )
        if problems:
            return _status(
                SteamOSState.needs_files,
                SteamOSVia.server_build,
                "Needs your game files",
                "The server builds it from your game files, and it doesn't have usable ones.",
                problems=problems,
                version=tag,
                build_id=build_id,
            )
        if builds_unavailable:
            return _status(
                SteamOSState.builds_off,
                SteamOSVia.server_build,
                "Server builds off",
                "It has to be compiled, and builds can't run on this server: "
                f"{builds_unavailable}.",
                problems=[builds_unavailable],
                version=tag,
            )
        if latest and latest.status == JobStatus.failed:
            first = (latest.error or "failed").splitlines()[0]
            return _status(
                SteamOSState.build_failed,
                SteamOSVia.server_build,
                "Build failed",
                f"The server's build of {tag} failed: {first}",
                problems=[first],
                version=tag,
                build_id=build_id,
            )
        return _status(
            SteamOSState.not_built,
            SteamOSVia.server_build,
            "Not built yet",
            f"The server can build {tag} for SteamOS; it hasn't yet.",
            version=tag,
            build_id=build_id,
        )

    # runtime / manual: the Linux release runs as is, with the game files placed.
    problems = input_problems(session, env, recomp, _needed_slots(recipe, None))
    if problems:
        return _status(
            SteamOSState.needs_files,
            SteamOSVia.release,
            "Needs your game files",
            "The Linux release runs as is, but needs your game files from the server.",
            problems=problems,
            version=tag,
        )
    manual = strategy == Strategy.manual
    return _status(
        SteamOSState.ready,
        SteamOSVia.release,
        "Linux release" if not manual else "Linux release (manual steps)",
        "The project's Linux release runs on SteamOS as is."
        if not manual
        else "The Linux release runs on SteamOS, with a few steps by hand.",
        version=tag,
    )
