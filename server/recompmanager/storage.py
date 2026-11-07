"""Library layout on disk and streaming downloads.

    /data/library/<game>/<recomp>/<platform>/<version>/<original-asset-name>
    /data/inputs/<game>/<input-id>/...
    /data/builds/<game>/<recomp>/<platform>/<version>/{work,output}/

Assets are kept as published (not pre-extracted). A recomp whose game isn't
known yet lives under ``_unsorted``; it moves when a game is assigned. A file
serving several platforms is hardlinked (or copied) into each platform folder.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import httpx

CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]+")


class DownloadError(Exception):
    pass


def safe_component(value: str) -> str:
    """A string that is safe as one path component on every OS."""
    s = _UNSAFE.sub("_", value).strip("._") or "_"
    return s[:120]


UNSORTED = "_unsorted"


def recomp_dir_rel(game_id: str | None, recomp_id: str) -> str:
    game = safe_component(game_id) if game_id else UNSORTED
    return f"library/{game}/{safe_component(recomp_id)}"


def version_dir_rel(game_id: str | None, recomp_id: str, platform: str, tag: str) -> str:
    base = recomp_dir_rel(game_id, recomp_id)
    return f"{base}/{safe_component(platform)}/{safe_component(tag)}"


def artifact_rel(
    game_id: str | None, recomp_id: str, platform: str, tag: str, asset_name: str
) -> str:
    return f"{version_dir_rel(game_id, recomp_id, platform, tag)}/{safe_component(asset_name)}"


def input_dir_rel(game_id: str, input_id: int) -> str:
    return f"inputs/{safe_component(game_id)}/{input_id}"


def build_dir_rel(game_id: str | None, recomp_id: str, platform: str, tag: str) -> str:
    """builds/<game>/<recomp>/<platform>/<version>: work/, output/ and build.log."""
    game = safe_component(game_id) if game_id else UNSORTED
    return (
        f"builds/{game}/{safe_component(recomp_id)}/{safe_component(platform)}/"
        f"{safe_component(tag)}"
    )


def link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink when the filesystem allows (no extra space), else copy."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:  # across filesystems
        shutil.move(str(src), str(dst))


def remove_empty_dirs(path: Path, stop: Path) -> None:
    """Remove ``path`` and its parents while they're empty, up to (not incl.) ``stop``."""
    stop = stop.resolve()
    p = path.resolve()
    while p != stop and p.is_relative_to(stop):
        try:
            p.rmdir()
        except OSError:
            return
        p = p.parent


def resolve(data_dir: Path, rel: str) -> Path:
    """Data-relative path → absolute, refusing anything that escapes the data dir."""
    root = data_dir.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise ValueError("path escapes the data directory")
    return p


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


async def download(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    size: int | None,
    sha256: str | None,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> str:
    """Stream ``url`` to ``dest`` and return its sha256.

    Resumes from ``dest.part`` with a Range request when a previous attempt was
    interrupted. Verifies size and, when given, sha256; on mismatch the partial
    file is deleted so the next attempt starts clean.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size is not None and have > size:
        part.unlink()
        have = 0

    h = hashlib.sha256()
    if have:  # rehash what we already have so the final digest covers everything
        with part.open("rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)

    headers = {"Accept": "application/octet-stream"}
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        async with client.stream(
            "GET", url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60, read=300)
        ) as resp:
            if resp.status_code == 416 and size is not None and have == size:
                pass  # already complete
            elif resp.status_code == 200 and have:
                # Server ignored the Range header: start over.
                have, h = 0, hashlib.sha256()
                part.unlink(missing_ok=True)
            elif resp.status_code not in (200, 206):
                raise DownloadError(f"Download failed: HTTP {resp.status_code}")
            if resp.status_code in (200, 206):
                total = size
                if total is None and (cl := resp.headers.get("content-length")):
                    total = have + int(cl)
                with part.open("ab") as f:
                    # No chunk_size: write bytes as they arrive, so an interrupted
                    # transfer keeps everything received for the resume.
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        if on_progress:
                            on_progress(have, total)
    except httpx.HTTPError as e:
        raise DownloadError(f"Download interrupted ({e.__class__.__name__}); it will resume") from e

    if size is not None and have != size:
        raise DownloadError(f"Size mismatch: expected {size} bytes, got {have}")
    digest = h.hexdigest()
    if sha256 is not None and digest != sha256.lower():
        part.unlink(missing_ok=True)
        raise DownloadError("Checksum mismatch: the download is corrupt or was changed upstream")
    os.replace(part, dest)
    return digest
