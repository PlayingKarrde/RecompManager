"""Recipe: the declarative definition of one game (``/config/recipes/<id>.yaml``)."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from recompmanager.platforms import is_platform

ID_RE = r"^[a-z0-9][a-z0-9-]{0,62}$"
REPO_RE = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Model):
    type: Literal["github"] = "github"  # gitlab/codeberg later
    repo: str = Field(pattern=REPO_RE, description="owner/name")
    channel: Literal["stable", "prerelease"] = Field(
        default="stable", description="prerelease also considers pre-releases"
    )
    auto_update: bool = True


class AssetRule(_Model):
    match: str = Field(description="Regular expression searched in release asset names")

    @field_validator("match")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e
        return v


class InputKind(StrEnum):
    none = "none"
    file = "file"
    folder = "folder"
    iso = "iso"


class InputVerify(_Model):
    sha1: list[str] = Field(default_factory=list)
    md5: list[str] = Field(default_factory=list)
    crc32: list[str] = Field(default_factory=list)
    required_paths: list[str] = Field(
        default_factory=list, description="Paths that must exist inside a folder input"
    )
    xbe_sha256: list[str] = Field(
        default_factory=list,
        description="sha256 of default.xbe inside an Xbox disc image. Tells region and "
        "revision apart however the image was dumped",
    )
    hashes_url: str | None = Field(
        default=None,
        description="URL of a JSON list of {name, sha1} fetched at refresh time "
        "(e.g. Ship of Harkinian's supportedHashes.json)",
    )
    hash_name_pattern: str | None = Field(
        default=None,
        description="Regex the matched hash-list entry's name must match "
        "(tells a Master Quest ROM from a normal one)",
    )
    version: str | None = Field(
        default=None, description="Required game version, e.g. 01.09 (PS4 APP_VER)"
    )
    filenames: list[str] = Field(
        default_factory=list,
        description="Names the input usually has (e.g. vf2.zip); helps the ROM library scan",
    )


class InputSlot(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    kind: InputKind = InputKind.file
    filename: str | None = None
    description: str = ""
    optional: bool = False
    verify: InputVerify = Field(default_factory=InputVerify)


class InputSpec(_Model):
    kind: InputKind = InputKind.none
    filename: str | None = None
    description: str = ""
    verify: InputVerify = Field(default_factory=InputVerify)
    slots: list[InputSlot] | None = Field(
        default=None, description="Several inputs (e.g. a normal and a Master Quest ROM)"
    )


class Strategy(StrEnum):
    runtime = "runtime"
    server_extract = "server_extract"
    server_build = "server_build"
    client_build = "client_build"
    manual = "manual"


class BuildSpec(_Model):
    """A server-side build or extraction, run in a sibling container.

    Inside the container: the release file is at ``$RELEASE`` (under /release, read-only),
    each input slot at ``$INPUT_<SLOT>`` (under /input, read-only), scratch space at
    ``$WORK`` (/work) and results go to ``$OUTPUT`` (/output). ``$VERSION`` and
    ``$PLATFORM`` are set too. The container runs as the server's own user.
    """

    builder: str = Field(
        description="Builder image: a folder with a Dockerfile, looked up in "
        "/config/recipes/builders/<name>/ and then the server's built-in builders"
    )
    command: list[str] = Field(min_length=1)
    release_platform: str | None = Field(
        default=None, description="Platform whose release file to use (default: the one built for)"
    )
    inputs: list[str] | None = Field(
        default=None, description="Input slots to mount (default: every non-optional slot)"
    )
    outputs: list[str] = Field(min_length=1, description="Glob patterns under /output to keep")
    network: bool = Field(
        default=False, description="Allow network access while building (e.g. to fetch tools)"
    )
    memory_gb: int | None = Field(default=None, ge=1, description="Memory limit")
    cpus: float | None = Field(default=None, gt=0, description="CPU limit")
    min_memory_gb: int = Field(default=0, ge=0, description="Refuse if the host has less")
    min_disk_gb: int = Field(default=0, ge=0, description="Refuse if /data has less free")
    timeout_minutes: int = Field(default=120, ge=1)


class PlatformPlan(_Model):
    strategy: Strategy = Strategy.runtime
    steps: list[dict[str, Any]] = Field(
        default_factory=list, description="Install-plan template (step types: see API docs)"
    )
    notes: str | None = None
    build: BuildSpec | None = Field(
        default=None, description="For server_build: how the server builds this platform"
    )
    fallback: PlatformPlan | None = Field(
        default=None,
        description="Plan to use while no server build/extraction exists (or the server "
        "can't build): usually client_build or runtime",
    )


class Launch(_Model):
    exe: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


def _check_platform_keys(keys: Any, allow_default: bool) -> None:
    for key in keys:
        if allow_default and key == "default":
            continue
        if not is_platform(key):
            raise ValueError(f"unknown platform key {key!r}")


class GameRef(_Model):
    """The original game a recomp runs. Recomps of the same game share its inputs."""

    id: str = Field(pattern=ID_RE)
    title: str = Field(min_length=1, max_length=200)
    system: str | None = Field(default=None, description="n64, arcade, ps4, switch, xbox…")
    identifiers: dict[str, str] = Field(
        default_factory=dict,
        description="Exact IDs, e.g. ps4_title_id: CUSA03173, switch_program_id: 0100…",
    )


class Recipe(_Model):
    id: str = Field(pattern=ID_RE)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    game: GameRef | None = Field(default=None, description="Which game this recomp runs")
    source: Source
    assets: dict[str, AssetRule] = Field(
        default_factory=dict, description="Platform key → asset name regex"
    )
    input: InputSpec = Field(default_factory=InputSpec)
    platforms: dict[str, PlatformPlan] = Field(
        default_factory=dict, description="Install strategy per platform key, or 'default'"
    )
    saves: dict[str, str] = Field(default_factory=dict)
    launch: dict[str, Launch] = Field(default_factory=dict)
    extract: BuildSpec | None = Field(
        default=None,
        description="For server_extract: one extraction per version whose outputs work on "
        "every platform (e.g. Ship of Harkinian's .o2r archives)",
    )
    unverified: list[str] = Field(
        default_factory=list,
        description="Facts in this recipe not yet confirmed, e.g. 'launch.macos-universal'",
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _platform_keys(self) -> Recipe:
        _check_platform_keys(self.assets, allow_default=False)
        _check_platform_keys(self.platforms, allow_default=True)
        _check_platform_keys(self.saves, allow_default=True)
        _check_platform_keys(self.launch, allow_default=True)
        return self
