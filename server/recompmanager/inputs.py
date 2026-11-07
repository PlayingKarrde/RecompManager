"""The inputs vault: the user's game dumps, how they get in, and whether they fit.

An input belongs to a game. It's either copied into ``/data/inputs`` (vault) or
read in place from ``/roms`` or ``/import`` (reference). Facts (hashes, PS4
param.sfo, Switch program ID, folder manifests) are worked out once and cached;
whether an input suits a recomp is evaluated from those facts and the recipe.

Paths of user files are never logged at info level.

Job handlers: ``import``, ``hash``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from recompmanager import events, formats, storage
from recompmanager.config import Env
from recompmanager.jobs import JobContext, JobError, job_handler
from recompmanager.models import (
    Game,
    HttpCache,
    Input,
    InputFile,
    InputKind,
    InputState,
    InputStorage,
    utcnow,
)
from recompmanager.recipes.schema import InputKind as SpecKind
from recompmanager.recipes.schema import InputSlot, InputVerify, Recipe
from recompmanager.recipes.store import RecipeError

IMPORT = "import"
HASH = "hash"


class InputError(Exception):
    """User-facing problem with a path or an input."""


# ---------------------------------------------------------------- paths


def source_roots(env: Env) -> dict[str, Path]:
    """Folders the server may read user files from (mounted read-only)."""
    return {
        name: p for name, p in (("import", env.import_dir), ("roms", env.roms_dir)) if p.is_dir()
    }


def check_source(env: Env, path: str) -> Path:
    """A path the user asked to import, confined to /import or /roms."""
    p = Path(path)
    if not p.is_absolute():
        raise InputError("Give the full path inside the container, e.g. /import/MyGame")
    p = p.resolve()
    for root in source_roots(env).values():
        if p == root.resolve() or p.is_relative_to(root.resolve()):
            if not p.exists():
                raise InputError("That path doesn't exist")
            return p
    raise InputError("Only paths under /import or /roms can be imported")


def input_path(env: Env, inp: Input) -> Path:
    if inp.storage == InputStorage.vault:
        return storage.resolve(env.data_dir, inp.path)
    return Path(inp.path)


# ---------------------------------------------------------------- analysis


def _fingerprint_file(p: Path) -> str:
    st = p.stat()
    return f"f:{st.st_size}:{st.st_mtime_ns}"


def analyze_file(p: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(hashes, facts) for a file input."""
    hashes = formats.hash_file(p)
    facts: dict[str, Any] = {}
    if "n64_order" in hashes:
        facts["n64_order"] = hashes.pop("n64_order")
        facts["sha1_z64"] = hashes.pop("sha1_z64")
    name = p.name.lower()
    if name.endswith(".zip"):
        try:
            facts["zip_entries"] = formats.zip_entries(p)
        except zipfile.BadZipFile:
            facts["zip_error"] = True
    if name.endswith((".iso", ".xiso")):
        facts["xbox_iso"] = formats.is_xbox_iso(p)
    if name.endswith(".pkg") and (cid := formats.ps4_pkg_content_id(p)):
        facts["ps4_pkg"] = {"content_id": cid, "title_id": formats.content_id_title(cid)}
    return hashes, facts


def walk_files(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            try:
                st = full.stat()
            except OSError:
                continue
            out.append((full.relative_to(root).as_posix(), full, st))
    return out


def folder_facts(root: Path) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if ps4 := formats.ps4_folder_facts(root):
        facts["ps4"] = ps4
    if sw := formats.switch_folder_facts(root):
        facts["switch"] = sw
    return facts


# ---------------------------------------------------------------- hash lists (e.g. SoH)


def cached_hash_list(session: Session, url: str) -> list[dict[str, Any]] | None:
    row = session.get(HttpCache, url)
    return row.body if row and isinstance(row.body, list) else None


async def fetch_hash_list(http: httpx.AsyncClient, session_factory, url: str) -> None:
    """Fetch a JSON list of {name, sha1}; keeps the cached copy on failure."""
    with session_factory() as s:
        row = s.get(HttpCache, url)
        etag = row.etag if row else None
    headers = {"If-None-Match": etag} if etag else {}
    try:
        resp = await http.get(url, headers=headers, timeout=30)
    except httpx.HTTPError:
        return
    if resp.status_code != 200:
        return
    try:
        data = resp.json()
    except ValueError:
        return
    if not isinstance(data, list):
        return
    with session_factory() as s:
        row = s.get(HttpCache, url)
        if row is None:
            s.add(HttpCache(url=url, etag=resp.headers.get("etag"), body=data, fetched_at=utcnow()))
        else:
            row.etag, row.body, row.fetched_at = resp.headers.get("etag"), data, utcnow()
        s.commit()


def hash_list_urls(recipes: list[Recipe]) -> set[str]:
    urls = set()
    for r in recipes:
        for spec in slots(r):
            if spec.verify.hashes_url:
                urls.add(spec.verify.hashes_url)
    return urls


# ---------------------------------------------------------------- verification


@dataclass(frozen=True)
class Verdict:
    slot: str  # "" when the recipe has a single input
    status: str  # verified | unverified | mismatch
    reason: str


def slots(recipe: Recipe) -> list[InputSlot]:
    """The recipe's input slots; a single-input recipe has one unnamed slot."""
    spec = recipe.input
    if spec.kind == SpecKind.none and not spec.slots:
        return []
    if spec.slots:
        return list(spec.slots)
    return [
        InputSlot(
            id="input",
            kind=spec.kind,
            filename=spec.filename,
            description=spec.description,
            verify=spec.verify,
        )
    ]


def _required_ok(root: Path, required: list[str]) -> list[str]:
    return [r for r in required if not (root / r).exists()]


def evaluate_slot(session: Session, env: Env, inp: Input, game: Game, slot: InputSlot) -> Verdict:
    sid = slot.id
    want_folder = slot.kind == SpecKind.folder
    if want_folder != (inp.kind == InputKind.folder):
        need = "a folder" if want_folder else "a file"
        return Verdict(sid, "mismatch", f"Needs {need}")
    v: InputVerify = slot.verify
    facts = inp.facts or {}

    # Exact identifiers win: PS4 title ID / Switch program ID.
    ids = game.identifiers or {}
    if inp.kind == InputKind.folder:
        missing = _required_ok(input_path(env, inp), v.required_paths)
        if missing:
            return Verdict(sid, "mismatch", f"Missing {', '.join(missing)}")
        ps4 = facts.get("ps4") or {}
        if want := ids.get("ps4_title_id"):
            got = ps4.get("TITLE_ID")
            if got and got != want:
                return Verdict(sid, "mismatch", f"This is {got}, not {want}")
            if got == want:
                have = ps4.get("APP_VER") or ps4.get("VERSION")
                if v.version and have and str(have) != v.version:
                    return Verdict(sid, "mismatch", f"Version {have}; needs {v.version}")
                reason = f"Title ID {got}" + (f", version {have}" if have else "")
                return Verdict(sid, "verified", reason)
        sw = facts.get("switch") or {}
        if want := ids.get("switch_program_id"):
            got = sw.get("program_id")
            if got and got.upper() != want.upper():
                return Verdict(sid, "mismatch", f"Program ID {got}, not {want}")
            if got:
                return Verdict(sid, "verified", f"Program ID {got} (game version not checked)")

    # Checksums.
    expected: list[tuple[str, str, str]] = []  # (algo, value, name)
    for algo in ("sha1", "md5", "crc32"):
        for h in getattr(v, algo):
            expected.append((algo, h.lower(), ""))
    list_missing = False
    if v.hashes_url:
        lst = cached_hash_list(session, v.hashes_url)
        if lst is None:
            list_missing = True
        else:
            for e in lst:
                if isinstance(e, dict) and e.get("sha1"):
                    expected.append(("sha1", str(e["sha1"]).lower(), str(e.get("name") or "")))
    if expected and inp.kind == InputKind.file:
        have = {
            "sha1": {x for x in (inp.sha1, facts.get("sha1_z64")) if x},
            "md5": {inp.md5} if inp.md5 else set(),
            "crc32": {inp.crc32} if inp.crc32 else set(),
        }
        for algo, value, name in expected:
            if value in have[algo]:
                if v.hash_name_pattern and not re.search(v.hash_name_pattern, name):
                    return Verdict(
                        sid, "mismatch", f"This is {name}, which belongs in another slot"
                    )
                return Verdict(sid, "verified", f"Matches {name}" if name else f"{algo} matches")
        if inp.sha1:  # hashed, and nothing matched
            return Verdict(sid, "mismatch", "Checksum isn't in the list of supported dumps")
    if list_missing:
        return Verdict(sid, "unverified", "The list of supported dumps hasn't been fetched yet")
    if v.required_paths and inp.kind == InputKind.folder:
        return Verdict(sid, "unverified", "Expected files are present; no checksums to compare")
    return Verdict(sid, "unverified", "No checksums to compare against")


_RANK = {"verified": 2, "unverified": 1, "mismatch": 0}


def evaluate(session: Session, env: Env, inp: Input, game: Game, recipe: Recipe) -> Verdict | None:
    """Best slot verdict for this input in this recipe; None if the recipe needs no input."""
    if inp.state != InputState.ready:
        return None
    verdicts = [evaluate_slot(session, env, inp, game, s) for s in slots(recipe)]
    if not verdicts:
        return None
    return max(verdicts, key=lambda d: _RANK[d.status])


def recipes_of(svc, game: Game) -> list[tuple[str, Recipe]]:
    out = []
    for r in game.recomps:
        try:
            out.append((r.id, svc.recipes.load(r.id)))
        except RecipeError:
            continue
    return out


def name_matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch(name.lower(), p.lower()) for p in patterns)


# ---------------------------------------------------------------- copying (in a thread)


class Stopped(Exception):
    pass


def _copy_file(src: Path, dst: Path, stop: threading.Event, tick: Callable[[int], None]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fi, dst.open("wb") as fo:
        while chunk := fi.read(formats.CHUNK):
            if stop.is_set():
                raise Stopped
            fo.write(chunk)
            tick(len(chunk))
    shutil.copystat(src, dst)


def _copy_tree(src: Path, dst: Path, stop: threading.Event, tick: Callable[[int], None]) -> None:
    for rel, full, _st in walk_files(src):
        _copy_file(full, dst / rel, stop, tick)


def _extract_zip_member(
    archive: Path, member: str, dst: Path, stop: threading.Event, tick: Callable[[int], None]
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z, z.open(member) as fi, dst.open("wb") as fo:
        while chunk := fi.read(formats.CHUNK):
            if stop.is_set():
                raise Stopped
            fo.write(chunk)
            tick(len(chunk))


def _safe_member(name: str) -> bool:
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts


def _extract_zip_all(
    archive: Path, dst: Path, stop: threading.Event, tick: Callable[[int], None]
) -> None:
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            if stop.is_set():
                raise Stopped
            if not _safe_member(info.filename):
                raise InputError(f"Unsafe path in archive: {info.filename}")
            target = dst / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as fi, target.open("wb") as fo:
                while chunk := fi.read(formats.CHUNK):
                    fo.write(chunk)
                    tick(len(chunk))


def _extract_7z(archive: Path, dst: Path, member: str | None) -> None:
    exe = shutil.which("7z") or shutil.which("7zz")
    if not exe:
        raise InputError("7z isn't installed on the server, so .7z archives can't be opened")
    dst.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "x", "-y", f"-o{dst}", str(archive)]
    if member:
        cmd.append(member)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise InputError(f"7z failed: {res.stderr.strip()[:300]}")


def single_top_folder(folder: Path) -> Path:
    """If an extracted archive holds just one folder, use that folder."""
    entries = [e for e in folder.iterdir() if not e.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return folder


async def _in_thread(ctx: JobContext, fn: Callable[[threading.Event], Any]) -> Any:
    """Run blocking work in a thread; a job cancel stops it at the next chunk."""
    stop = threading.Event()
    task = asyncio.ensure_future(asyncio.to_thread(fn, stop))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        with contextlib.suppress(Stopped, Exception):
            await task
        raise


class _Ticker:
    """Thread-safe byte counter feeding job progress."""

    def __init__(self, ctx: JobContext, total: int, message: str) -> None:
        self.ctx, self.total, self.message = ctx, max(total, 1), message
        self.done = 0
        self._last = 0.0

    def __call__(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last > 0.5:
            self._last = now
            self.ctx.progress(min(self.done / self.total, 0.99), self.message)


def _tree_size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(st.st_size for _r, _f, st in walk_files(p))


# ---------------------------------------------------------------- jobs


def _publish(ctx: JobContext, game_id: str) -> None:
    ctx.services.events.publish(events.GAME_UPDATED, {"id": game_id})


@job_handler(IMPORT)
async def import_input(ctx: JobContext) -> dict[str, Any]:
    """Bring a dump in from /import or /roms.

    Params: source (path), mode (copy | move | reference), member (an entry
    inside a .zip/.7z to take), extract (unpack a whole archive into a folder),
    candidate_id (the scan suggestion this came from).
    """
    game_id = ctx.game_id
    assert game_id is not None
    env = ctx.services.env
    try:
        src = check_source(env, ctx.params["source"])
    except InputError as e:
        raise JobError(str(e)) from e
    mode = ctx.params.get("mode", "copy")
    member = ctx.params.get("member") or None
    extract = bool(ctx.params.get("extract"))
    if (member or extract) and mode == "reference":
        mode = "copy"  # an archive entry has to be unpacked somewhere

    with ctx.session() as s:
        inp = Input(
            game_id=game_id,
            name=PurePosixPath(member).name if member else src.name,
            kind=InputKind.folder if (src.is_dir() or extract) else InputKind.file,
            storage=InputStorage.reference if mode == "reference" else InputStorage.vault,
            path="",
            state=InputState.pending,
            source=ctx.params.get("origin", "import"),
            facts={"origin": {"path": str(src), "member": member or ""}},
        )
        s.add(inp)
        s.flush()
        input_id = inp.id
        vault_rel = storage.input_dir_rel(game_id, input_id)
    ctx.params["input_id"] = input_id
    _publish(ctx, game_id)
    vault = storage.resolve(env.data_dir, vault_rel)

    try:
        if mode == "reference":
            rel = str(src)
            ctx.log("Using the dump in place (not copied)")
        elif member:
            dst = vault / PurePosixPath(member).name
            if src.suffix.lower() == ".7z":
                ctx.progress(None, "Extracting from archive")
                await _in_thread(ctx, lambda stop: _extract_7z(src, vault, member))
                extracted = vault / member
                if extracted != dst:
                    storage.move(extracted, dst)
            else:
                size = next((e["size"] for e in formats.zip_entries(src) if e["name"] == member), 0)
                tick = _Ticker(ctx, size, "Extracting from archive")
                await _in_thread(
                    ctx, lambda stop: _extract_zip_member(src, member, dst, stop, tick)
                )
            rel = dst.relative_to(env.data_dir.resolve()).as_posix()
            ctx.log("Extracted one file from an archive")
        elif extract:
            out = vault / "contents"
            if src.suffix.lower() == ".7z":
                ctx.progress(None, "Extracting archive")
                await _in_thread(ctx, lambda stop: _extract_7z(src, out, None))
            else:
                total = sum(e["size"] for e in formats.zip_entries(src))
                tick = _Ticker(ctx, total, "Extracting archive")
                await _in_thread(ctx, lambda stop: _extract_zip_all(src, out, stop, tick))
            rel = single_top_folder(out).relative_to(env.data_dir.resolve()).as_posix()
            ctx.log("Extracted the archive into the vault")
        else:
            dst = vault / src.name
            moved = False
            if mode == "move":
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(src, dst)
                    moved = True
                except OSError:
                    ctx.log("Couldn't move (read-only or another disk); copying instead")
            if not moved:
                tick = _Ticker(ctx, _tree_size(src), "Copying")
                if src.is_dir():
                    await _in_thread(ctx, lambda stop: _copy_tree(src, dst, stop, tick))
                else:
                    await _in_thread(ctx, lambda stop: _copy_file(src, dst, stop, tick))
            rel = dst.relative_to(env.data_dir.resolve()).as_posix()
            ctx.log("Moved into the vault" if moved else "Copied into the vault")
    except (InputError, zipfile.BadZipFile, KeyError, OSError) as e:
        storage.remove_tree(vault)
        with ctx.session() as s:
            if inp := s.get(Input, input_id):
                inp.state, inp.error = InputState.failed, str(e)
        _publish(ctx, game_id)
        raise JobError(f"Import failed: {e}") from e
    except BaseException:
        storage.remove_tree(vault)
        with ctx.session() as s:
            if inp := s.get(Input, input_id):
                s.delete(inp)
        _publish(ctx, game_id)
        raise

    with ctx.session() as s:
        inp = s.get(Input, input_id)
        assert inp is not None
        inp.path = rel
        if cid := ctx.params.get("candidate_id"):
            from recompmanager.models import CandidateState, ScanCandidate

            if cand := s.get(ScanCandidate, cid):
                cand.state, cand.input_id = CandidateState.accepted, input_id
    await analyze(ctx, input_id)
    return {"input_id": input_id}


@job_handler(HASH)
async def hash_input(ctx: JobContext) -> dict[str, Any]:
    """Work out an input's hashes and facts again (params: input_id, force)."""
    input_id = int(ctx.params["input_id"])
    await analyze(ctx, input_id, force=bool(ctx.params.get("force")))
    return {"input_id": input_id}


async def analyze(ctx: JobContext, input_id: int, force: bool = False) -> None:
    env = ctx.services.env
    with ctx.session() as s:
        inp = s.get(Input, input_id)
        if inp is None:
            raise JobError("The input was removed")
        p = input_path(env, inp)
        kind, old_fp, game_id = inp.kind, inp.fingerprint, inp.game_id
        keep = {k: v for k, v in (inp.facts or {}).items() if k == "origin"}
        cache = {
            f.path: (f.size, f.mtime, f.sha256)
            for f in s.scalars(select(InputFile).where(InputFile.input_id == input_id))
        }
        game = s.get(Game, game_id)
        urls = hash_list_urls([r for _id, r in recipes_of(ctx.services, game)]) if game else set()

    for url in urls:
        await fetch_hash_list(ctx.services.http, ctx.services.sessionmaker, url)

    if not p.exists():
        with ctx.session() as s:
            if inp := s.get(Input, input_id):
                inp.state, inp.error = InputState.missing, "The file or folder is gone"
        _publish(ctx, game_id)
        raise JobError("The input's file or folder no longer exists")

    if kind == InputKind.file:
        fp = _fingerprint_file(p)
        if fp == old_fp and not force:
            ctx.log("Unchanged since it was last hashed")
        else:
            ctx.progress(None, "Hashing")
            ctx.log(f"Hashing {p.stat().st_size / 1024**2:.1f} MiB")
            hashes, facts = await asyncio.to_thread(analyze_file, p)
            with ctx.session() as s:
                inp = s.get(Input, input_id)
                assert inp is not None
                inp.size = hashes["size"]
                inp.file_count = 1
                inp.sha1, inp.md5 = hashes["sha1"], hashes["md5"]
                inp.crc32, inp.sha256 = hashes["crc32"], hashes["sha256"]
                inp.facts, inp.fingerprint, inp.hashed_at = {**facts, **keep}, fp, utcnow()
    else:
        files = await asyncio.to_thread(walk_files, p)
        total = sum(st.st_size for _r, _f, st in files) or 1
        newest = max((st.st_mtime for _r, _f, st in files), default=0)
        fp = f"d:{len(files)}:{total}:{newest}"
        rehash = [
            (rel, full, st)
            for rel, full, st in files
            if force
            or (c := cache.get(rel)) is None
            or c[0] != st.st_size
            or c[1] != st.st_mtime
            or c[2] is None
        ]
        ctx.log(f"{len(files)} files, {total / 1024**3:.2f} GiB; {len(rehash)} to hash")
        done = 0
        batch: list[tuple[str, int, float, str]] = []

        def flush_batch() -> None:
            with ctx.session() as s:
                for rel, size, mtime, sha in batch:
                    row = s.get(InputFile, (input_id, rel))
                    if row is None:
                        s.add(
                            InputFile(
                                input_id=input_id, path=rel, size=size, mtime=mtime, sha256=sha
                            )
                        )
                    else:
                        row.size, row.mtime, row.sha256 = size, mtime, sha
            batch.clear()

        rehash_total = sum(st.st_size for _r, _f, st in rehash) or 1
        for rel, full, st in rehash:
            sha = await asyncio.to_thread(formats.sha256_file, full)
            batch.append((rel, st.st_size, st.st_mtime, sha))
            done += st.st_size
            ctx.progress(done / rehash_total, "Hashing files")
            if len(batch) >= 500:
                flush_batch()
        flush_batch()
        present = {rel for rel, _f, _st in files}
        stale = [r for r in cache if r not in present]
        facts = await asyncio.to_thread(folder_facts, p)
        with ctx.session() as s:
            for rel in stale:
                if row := s.get(InputFile, (input_id, rel)):
                    s.delete(row)
            inp = s.get(Input, input_id)
            assert inp is not None
            inp.size, inp.file_count = total if files else 0, len(files)
            inp.facts, inp.fingerprint, inp.hashed_at = {**facts, **keep}, fp, utcnow()
    with ctx.session() as s:
        inp = s.get(Input, input_id)
        assert inp is not None
        inp.state, inp.error = InputState.ready, None
    ctx.progress(1.0, "Ready")
    _publish(ctx, game_id)
