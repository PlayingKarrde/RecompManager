"""Install plans: what a client must do to install a recomp on one platform.

The server works everything out (which file, which input, where things go);
clients just carry out a short list of typed steps. Placeholders ``{install}``,
``{data}`` and ``{home}`` are left for the client to fill in. Every step is
safe to run twice.

Strategy fallback (CLAUDE.md): server_build / server_extract → client_build →
runtime → manual. A server_* platform uses what the server built for that version
(``place_output`` steps); until that exists it uses the recipe's ``fallback`` plan,
or a manual plan that still offers the release files and the input.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager.config import Env
from recompmanager.inputs import evaluate_slot, slots
from recompmanager.models import (
    Artifact,
    ArtifactState,
    Build,
    Input,
    InputKind,
    InputState,
    JobStatus,
    Recomp,
    Version,
    VersionState,
)
from recompmanager.platforms import fallbacks
from recompmanager.recipes.schema import InputSlot, PlatformPlan, Recipe, Strategy

API = "/api/v1"


# ---------------------------------------------------------------- step types


class DownloadArtifactStep(BaseModel):
    type: Literal["download_artifact"] = "download_artifact"
    artifact_id: int
    name: str = Field(description="Original file name")
    url: str = Field(description="Relative to the server; supports Range for resuming")
    sha256: str
    size: int
    to: str = Field(description="File path, or folder when extract is true")
    extract: bool = Field(description="Unpack (.zip, .tar.*, .7z) into 'to' instead of saving")
    strip_root: bool = Field(
        default=False, description="When the archive holds a single top folder, unpack its contents"
    )


class DownloadInputStep(BaseModel):
    type: Literal["download_input"] = "download_input"
    input_id: int
    slot: str = Field(description="Which of the recipe's inputs this is")
    kind: InputKind
    name: str
    status: Literal["verified", "unverified"]
    url: str = Field(description="File: the file (Range supported). Folder: a tar stream")
    manifest_url: str | None = Field(
        default=None, description="Folder inputs: file list with sha256, for per-file download"
    )
    files_url: str | None = Field(
        default=None, description="Folder inputs: prefix to fetch single files (append the path)"
    )
    sha256: str | None = Field(default=None, description="File inputs only")
    size: int
    to: str = Field(description="File path, or folder for folder inputs")


class WriteFileStep(BaseModel):
    type: Literal["write_file"] = "write_file"
    path: str
    content: str


class ChmodStep(BaseModel):
    type: Literal["chmod"] = "chmod"
    path: str
    mode: str = Field(description="Octal, e.g. 755")


class RunStep(BaseModel):
    type: Literal["run"] = "run"
    command: str
    cwd: str
    confirm: Literal[True] = Field(
        default=True, description="Clients must ask the user before running this"
    )


class InstructionsStep(BaseModel):
    type: Literal["instructions"] = "instructions"
    markdown: str


Step = Annotated[
    DownloadArtifactStep
    | DownloadInputStep
    | WriteFileStep
    | ChmodStep
    | RunStep
    | InstructionsStep,
    Field(discriminator="type"),
]


class LaunchInfo(BaseModel):
    exe: str = Field(description="Relative to {install} unless it starts with a placeholder")
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class SavesInfo(BaseModel):
    path: str


class InstallPlan(BaseModel):
    recomp_id: str
    game_id: str | None
    name: str
    version: str | None
    requested_platform: str
    platform: str | None = Field(description="The platform key used, after fallback")
    strategy: Strategy = Field(description="The strategy used, after fallback")
    ready: bool = Field(description="False when something blocks the install (see problems)")
    problems: list[str] = Field(description="What blocks the install; fix these on the server")
    warnings: list[str]
    notes: str | None
    steps: list[Step]
    launch: LaunchInfo | None
    saves: SavesInfo | None


# ---------------------------------------------------------------- building


def _pick(mapping: dict[str, Any], keys: list[str]) -> tuple[str | None, Any]:
    for k in [*keys, "default"]:
        if k in mapping:
            return k, mapping[k]
    return None, None


def _artifact_for(version: Version, keys: list[str]) -> Artifact | None:
    by_platform = {
        a.platform: a
        for a in version.artifacts
        if a.state == ArtifactState.downloaded and a.origin == "release"
    }
    for k in keys:
        if k in by_platform:
            return by_platform[k]
    return None


def _build_outputs(version: Version, keys: list[str]) -> list[Artifact]:
    """Files the server built for this version, for the first platform key that has any."""
    for k in keys:
        found = [
            a
            for a in version.artifacts
            if a.origin == "build" and a.platform == k and a.state == ArtifactState.downloaded
        ]
        if found:
            return sorted(found, key=lambda a: a.name)
    return []


def _building(session: Session, recomp: Recomp, tag: str | None, keys: list[str]) -> int | None:
    b = session.scalar(
        select(Build).where(
            Build.recomp_id == recomp.id,
            Build.version_tag == tag,
            Build.platform.in_(keys),
            Build.status.in_([JobStatus.queued, JobStatus.running]),
        )
    )
    return b.id if b else None


def best_input(
    session: Session, env: Env, recomp: Recomp, slot: InputSlot
) -> tuple[Input | None, str]:
    """Best ready input of the game for one slot: verified, else unverified."""
    if recomp.game is None:
        return None, ""
    best: tuple[int, Input | None, str] = (-1, None, "")
    for inp in recomp.game.inputs:
        if inp.state != InputState.ready:
            continue
        v = evaluate_slot(session, env, inp, recomp.game, slot)
        rank = {"verified": 2, "unverified": 1}.get(v.status, -1)
        if rank > best[0]:
            best = (rank, inp, v.status)
    return best[1], best[2]


def _input_step(inp: Input, slot: str, status: str, to: str) -> DownloadInputStep:
    folder = inp.kind == InputKind.folder
    return DownloadInputStep(
        input_id=inp.id,
        slot=slot,
        kind=inp.kind,
        name=inp.name,
        status=status,  # type: ignore[arg-type]
        url=f"{API}/inputs/{inp.id}/download",
        manifest_url=f"{API}/inputs/{inp.id}/manifest" if folder else None,
        files_url=f"{API}/inputs/{inp.id}/files/" if folder else None,
        sha256=None if folder else inp.sha256,
        size=inp.size,
        to=to,
    )


def build_plan(
    session: Session,
    env: Env,
    recomp: Recomp,
    recipe: Recipe,
    platform: str,
    version_tag: str | None = None,
) -> InstallPlan:
    problems: list[str] = []
    warnings: list[str] = []
    keys = fallbacks(platform)

    tag = version_tag or recomp.current_version
    version = next((v for v in recomp.versions if v.tag == tag), None) if tag else None
    if version is None:
        problems.append(
            f"Version {tag} isn't known" if tag else "Nothing is downloaded on the server yet"
        )
    elif version.state != VersionState.downloaded:
        problems.append(f"Version {tag} isn't downloaded on the server ({version.state})")

    artifact = _artifact_for(version, keys) if version else None
    asset_key = artifact.platform if artifact else None
    plan_key, pplan = _pick(recipe.platforms, keys)
    pplan = pplan or PlatformPlan()
    if version is not None and artifact is None and recipe.assets:
        problems.append(f"{recipe.name} has no release file for {platform}")

    strategy = pplan.strategy
    template = list(pplan.steps)
    outputs: list[Artifact] = []
    if strategy in (Strategy.server_build, Strategy.server_extract):
        extract = strategy == Strategy.server_extract
        out_keys = ["any"] if extract else keys
        outputs = _build_outputs(version, out_keys) if version else []
        if not outputs:
            what = "extracted the game data" if extract else "built this platform"
            busy = _building(session, recomp, tag, out_keys)
            if busy:
                warnings.append(f"The server is working on it (build #{busy}).")
            if pplan.fallback is not None:
                warnings.append(
                    f"The server hasn't {what} yet, so this uses the "
                    f"{pplan.fallback.strategy.replace('_', ' ')} plan instead."
                )
                strategy = pplan.fallback.strategy
                template = list(pplan.fallback.steps)
                pplan = pplan.fallback.model_copy(
                    update={"notes": pplan.fallback.notes or pplan.notes}
                )
            else:
                warnings.append(f"The server hasn't {what} yet. Falling back to a manual install.")
                strategy = Strategy.manual
                template = [{"download_artifact": {"to": "{install}", "extract": True}}]

    steps: list[Any] = []
    used_slots: set[str] = set()
    recipe_slots = {s.id: s for s in slots(recipe)}
    default_slot = next(iter(recipe_slots), None)

    for raw in template:
        if not isinstance(raw, dict) or len(raw) != 1:
            problems.append(f"Recipe step isn't understood: {raw!r}")
            continue
        ((kind, args),) = raw.items()
        args = args or {}
        if kind == "download_artifact":
            if artifact is None or artifact.sha256 is None:
                continue
            steps.append(
                DownloadArtifactStep(
                    artifact_id=artifact.id,
                    name=artifact.name,
                    url=f"{API}/artifacts/{artifact.id}/download",
                    sha256=artifact.sha256,
                    size=artifact.size,
                    to=args.get("to", "{install}"),
                    extract=bool(args.get("extract", False)),
                    strip_root=bool(args.get("strip_root", False)),
                )
            )
        elif kind == "place_output":
            pattern = args.get("name", "*")
            matched = [a for a in outputs if fnmatch(a.name, pattern) and a.sha256]
            if not matched:
                problems.append(f"The server build has no output matching {pattern!r}")
            for a in matched:
                steps.append(
                    DownloadArtifactStep(
                        artifact_id=a.id,
                        name=a.name,
                        url=f"{API}/artifacts/{a.id}/download",
                        sha256=a.sha256,
                        size=a.size,
                        to=args.get("to", "{install}"),
                        extract=bool(args.get("extract", False)),
                        strip_root=bool(args.get("strip_root", False)),
                    )
                )
        elif kind == "place_input":
            sid = args.get("slot") or default_slot
            slot = recipe_slots.get(sid) if sid else None
            if slot is None:
                problems.append("The recipe places an input, but defines none")
                continue
            used_slots.add(slot.id)
            inp, status = best_input(session, env, recomp, slot)
            if inp is None:
                what = slot.description or slot.id
                if slot.optional:
                    warnings.append(f"Optional input not added on the server: {what}")
                else:
                    problems.append(f"Add the game input on the server: {what}")
                continue
            if status == "unverified":
                warnings.append(f"The input {inp.name!r} couldn't be verified; it may not work")
            steps.append(_input_step(inp, slot.id, status, args.get("to", "{install}")))
        elif kind == "write_file":
            steps.append(WriteFileStep(path=args["path"], content=str(args.get("content", ""))))
        elif kind == "chmod":
            steps.append(ChmodStep(path=args["path"], mode=str(args.get("mode", "755"))))
        elif kind == "run":
            steps.append(RunStep(command=args["command"], cwd=args.get("cwd", "{install}")))
        elif kind == "instructions":
            steps.append(InstructionsStep(markdown=args["markdown"]))
        else:
            problems.append(f"Unknown step type {kind!r} in the recipe")

    if strategy == Strategy.manual:
        # Always offer the inputs as the last fallback.
        for sid, slot in recipe_slots.items():
            if sid in used_slots:
                continue
            inp, status = best_input(session, env, recomp, slot)
            if inp is not None:
                steps.append(_input_step(inp, sid, status, f"{{data}}/{recomp.id}/input"))
        if pplan.notes:
            steps.append(InstructionsStep(markdown=pplan.notes))

    _lk, launch = _pick(recipe.launch, keys)
    _sk, saves = _pick(recipe.saves, keys)
    return InstallPlan(
        recomp_id=recomp.id,
        game_id=recomp.game_id,
        name=recipe.name,
        version=version.tag if version else tag,
        requested_platform=platform,
        platform=asset_key or plan_key,
        strategy=strategy,
        ready=not problems,
        problems=problems,
        warnings=warnings,
        notes=pplan.notes,
        steps=steps,
        launch=LaunchInfo(exe=launch.exe, args=launch.args, env=launch.env) if launch else None,
        saves=SavesInfo(path=saves) if saves and saves != "unknown" else None,
    )
