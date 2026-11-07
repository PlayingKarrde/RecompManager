"""ROM library scan: find each game's inputs in /roms (and /import) by itself.

The scan walks the library recursively and looks for:

- **Files** (N64 ROMs and the like): hashed when their size and extension fit,
  then compared with the recipes' hash lists. Byte order is normalised first.
- **Files inside .zip archives**: entries are listed from the zip index and
  hashed straight out of the archive, without unpacking anything to disk.
- **Named files** a recipe expects as-is (VF2's MAME ``vf2.zip``).
- **Folders** with a known shape: PS4 dumps (``eboot.bin`` + ``sce_sys/param.sfo``,
  title ID read from param.sfo) and Switch dumps (``exefs/`` + ``romfs/``,
  program ID read from main.npdm). The scan doesn't descend into a folder once
  it matches, so a 300,000-file romfs isn't walked.
- **Disc images** (.iso/.xiso) whose name matches the game.
- **Encrypted packages** (PS4 .pkg, Switch .nsp/.xci): reported as unusable,
  since they'd need console keys to unpack.

Results become suggestions (``scan_candidates``). Hash- or ID-verified ones can
be accepted automatically, as references (nothing copied). What was learned
about each file is cached by path, size and mtime, so rescans are cheap.

Job handler: ``scan``.
"""

from __future__ import annotations

import os
import re
import threading
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from sqlalchemy import select

from recompmanager import app_settings, events, formats
from recompmanager.inputs import (
    IMPORT,
    Stopped,
    _in_thread,
    cached_hash_list,
    fetch_hash_list,
    hash_list_urls,
    recipes_of,
    slots,
    source_roots,
)
from recompmanager.jobs import JobContext, job_handler
from recompmanager.models import (
    CandidateState,
    Game,
    Input,
    ScanCandidate,
    ScanFile,
    utcnow,
)
from recompmanager.recipes.schema import InputKind as SpecKind

SCAN = "scan"

# Cartridge ROMs worth hashing. Not .bin: arcade sets and disc images use it too.
ROM_EXTS = {".z64", ".n64", ".v64", ".sfc", ".smc", ".gba", ".nds", ".gb", ".gbc"}
ROM_MAX = 512 * 1024**2  # don't hash "ROMs" bigger than this while scanning
ISO_EXTS = {".iso", ".xiso"}
PKG_EXTS = {".pkg"}
SWITCH_EXTS = {".nsp", ".xci", ".nsz", ".xcz"}
SKIP_DIRS = {"$recycle.bin", "system volume information", "@eadir", ".trash", ".recycle"}
_STOPWORDS = {"the", "of", "a", "an", "and", "legend", "zelda"}


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))


def title_words(title: str) -> list[str]:
    """Distinctive words of a title: 'The Legend of Zelda: Majora's Mask' → majoras, mask."""
    words = [w for w in _norm_words(title) if w not in _STOPWORDS]
    return words or _norm_words(title)


def title_in_name(words: list[str], name: str) -> bool:
    have = set(_norm_words(name))
    return bool(words) and all(w in have for w in words)


@dataclass
class GameNeeds:
    """What one game's recomps look for, merged across recipes."""

    game: Game
    words: list[str]
    file_slots: bool = False
    folder_slots: bool = False
    iso_slots: bool = False
    filenames: list[str] = field(default_factory=list)
    required_paths: list[list[str]] = field(default_factory=list)
    sha1: dict[str, str] = field(default_factory=dict)  # sha1 → name (from hash lists)
    has_hashes: bool = False


@dataclass
class Found:
    game_id: str
    path: str
    member: str
    kind: str  # file | folder | archive_member | package
    confidence: str  # verified | likely | unusable
    reason: str
    size: int


def build_needs(session, svc) -> list[GameNeeds]:
    out = []
    for game in session.scalars(select(Game)):
        needs = GameNeeds(game=game, words=title_words(game.title))
        for _rid, recipe in recipes_of(svc, game):
            for slot in slots(recipe):
                v = slot.verify
                if slot.kind == SpecKind.folder:
                    needs.folder_slots = True
                    if v.required_paths:
                        needs.required_paths.append(v.required_paths)
                elif slot.kind == SpecKind.iso:
                    needs.iso_slots = True
                else:
                    needs.file_slots = True
                needs.filenames += v.filenames + ([slot.filename] if slot.filename else [])
                for h in v.sha1:
                    needs.sha1[h.lower()] = ""
                    needs.has_hashes = True
                if v.hashes_url and (lst := cached_hash_list(session, v.hashes_url)):
                    for e in lst:
                        if isinstance(e, dict) and e.get("sha1"):
                            needs.sha1[str(e["sha1"]).lower()] = str(e.get("name") or "")
                            needs.has_hashes = True
        if needs.file_slots or needs.folder_slots or needs.iso_slots:
            out.append(needs)
    return out


class Scanner:
    """Walks the library in a worker thread. Uses and fills the ScanFile cache."""

    def __init__(self, needs: list[GameNeeds], cache: dict[str, ScanFile], stop: threading.Event):
        self.needs = needs
        self.cache = cache
        self.stop = stop
        self.found: list[Found] = []
        self.cache_updates: dict[str, tuple[int, float, dict[str, Any]]] = {}
        self.seen = 0
        self.hashed = 0
        self.by_sha1: dict[str, list[tuple[GameNeeds, str]]] = defaultdict(list)
        for n in needs:
            for h, name in n.sha1.items():
                self.by_sha1[h].append((n, name))
        self.wants_roms = any(n.file_slots for n in needs)

    # -------------------------------------------------- cache

    def _cached(self, key: str, st: os.stat_result) -> dict[str, Any] | None:
        row = self.cache.get(key)
        if row is not None and row.size == st.st_size and row.mtime == st.st_mtime:
            return row.info
        return None

    def _remember(self, key: str, st: os.stat_result, info: dict[str, Any]) -> None:
        self.cache_updates[key] = (st.st_size, st.st_mtime, info)

    # -------------------------------------------------- walk

    def walk(self, root: Path) -> None:
        stack = [root]
        while stack:
            if self.stop.is_set():
                raise Stopped
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in sorted(entries, key=lambda e: e.name):
                if e.name.startswith(".") or e.name.lower() in SKIP_DIRS:
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not self.folder(Path(e.path)):
                        stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    self.seen += 1
                    try:
                        self.file(Path(e.path), e.stat())
                    except (OSError, zipfile.BadZipFile, ValueError):
                        continue

    # -------------------------------------------------- folders

    def folder(self, p: Path) -> bool:
        """True when ``p`` is a dump (so the walk shouldn't go inside)."""
        if ps4 := formats.ps4_folder_facts(p):
            tid = ps4.get("TITLE_ID")
            for n in self.needs:
                want = (n.game.identifiers or {}).get("ps4_title_id")
                if n.folder_slots and want and tid == want:
                    ver = ps4.get("APP_VER") or ps4.get("VERSION")
                    self.add(n, p, "", "folder", "verified", f"PS4 dump {tid}, version {ver}", 0)
            return True
        if sw := formats.switch_folder_facts(p):
            pid = (sw.get("program_id") or "").upper()
            for n in self.needs:
                want = ((n.game.identifiers or {}).get("switch_program_id") or "").upper()
                if n.folder_slots and want and pid == want:
                    self.add(n, p, "", "folder", "verified", f"Switch dump, program ID {pid}", 0)
            return True
        matched = False
        for n in self.needs:
            for req in n.required_paths:
                if req and all((p / r).exists() for r in req):
                    self.add(n, p, "", "folder", "likely", f"Has {', '.join(req)}", 0)
                    matched = True
        return matched

    # -------------------------------------------------- files

    def file(self, p: Path, st: os.stat_result) -> None:
        name = p.name
        ext = p.suffix.lower()
        for n in self.needs:
            if any(fnmatch(name.lower(), f.lower()) for f in n.filenames if "*" not in f) or any(
                fnmatch(name.lower(), f.lower()) and title_in_name(n.words, name)
                for f in n.filenames
                if "*" in f
            ):
                self.add(n, p, "", "file", "likely", f"Named {name}", st.st_size)
        if ext in ROM_EXTS and self.wants_roms and 0 < st.st_size <= ROM_MAX:
            self.rom(p, st)
        elif ext == ".zip" and self.wants_roms:
            self.zip(p, st)
        elif ext in ISO_EXTS:
            for n in self.needs:
                if n.iso_slots and title_in_name(n.words, name):
                    xbox = self._info(p, st, lambda: {"xbox_iso": formats.is_xbox_iso(p)})
                    reason = "Xbox disc image" if xbox.get("xbox_iso") else "Disc image"
                    self.add(
                        n, p, "", "file", "likely", f"{reason} named like the game", st.st_size
                    )
        elif ext in PKG_EXTS:
            info = self._info(p, st, lambda: {"content_id": formats.ps4_pkg_content_id(p)})
            tid = formats.content_id_title(info.get("content_id") or "")
            for n in self.needs:
                if tid and tid == (n.game.identifiers or {}).get("ps4_title_id"):
                    self.add(
                        n,
                        p,
                        "",
                        "package",
                        "unusable",
                        f"PS4 package {tid}: encrypted; extract it to a folder first",
                        st.st_size,
                    )
        elif ext in SWITCH_EXTS:
            tid = formats.switch_title_id_from_name(name)
            for n in self.needs:
                want = ((n.game.identifiers or {}).get("switch_program_id") or "").upper()
                if (tid and tid == want) or (want and title_in_name(n.words, name)):
                    self.add(
                        n,
                        p,
                        "",
                        "package",
                        "unusable",
                        "Switch package: encrypted; dump it as exefs/romfs folders",
                        st.st_size,
                    )

    def _info(self, p: Path, st: os.stat_result, compute) -> dict[str, Any]:
        key = str(p)
        info = self._cached(key, st)
        if info is None:
            info = compute()
            self._remember(key, st, info)
        return info

    def _match_hashes(
        self, info: dict[str, Any], p: Path, member: str, kind: str, size: int
    ) -> bool:
        hit = False
        for h in {info.get("sha1"), info.get("sha1_z64")} - {None}:
            for n, name in self.by_sha1.get(h, []):
                self.add(
                    n,
                    p,
                    member,
                    kind,
                    "verified",
                    f"Matches {name}" if name else "Checksum matches",
                    size,
                )
                hit = True
        return hit

    def _title_guess(self, p: Path, member: str, kind: str, size: int, label: str) -> None:
        for n in self.needs:
            if n.file_slots and not n.has_hashes and title_in_name(n.words, label):
                self.add(n, p, member, kind, "likely", "Named like the game", size)

    def rom(self, p: Path, st: os.stat_result) -> None:
        def compute() -> dict[str, Any]:
            self.hashed += 1
            h = formats.hash_file(p)
            return {k: h[k] for k in ("sha1", "sha1_z64") if k in h}

        if not self.by_sha1:  # nothing to compare against: names only
            self._title_guess(p, "", "file", st.st_size, p.name)
            return
        info = self._info(p, st, compute)
        if not self._match_hashes(info, p, "", "file", st.st_size):
            self._title_guess(p, "", "file", st.st_size, p.name)

    def zip(self, p: Path, st: os.stat_result) -> None:
        listing = self._info(p, st, lambda: {"entries": formats.zip_entries(p)})
        for e in listing.get("entries", []):
            if Path(e["name"]).suffix.lower() not in ROM_EXTS or not 0 < e["size"] <= ROM_MAX:
                continue
            if not self.by_sha1:
                self._title_guess(p, e["name"], "archive_member", e["size"], e["name"])
                continue
            key = f"{p}::{e['name']}"

            def compute(e=e) -> dict[str, Any]:
                self.hashed += 1
                h = formats.hash_zip_member(p, e["name"])
                return {k: h[k] for k in ("sha1", "sha1_z64") if k in h}

            info = self._info(Path(key), st, compute)
            if not self._match_hashes(info, p, e["name"], "archive_member", e["size"]):
                self._title_guess(p, e["name"], "archive_member", e["size"], e["name"])

    # -------------------------------------------------- results

    def add(self, n: GameNeeds, p: Path, member: str, kind: str, conf: str, reason: str, size: int):
        self.found.append(Found(n.game.id, str(p), member, kind, conf, reason[:500], size))


def _dedupe(found: list[Found]) -> list[Found]:
    rank = {"verified": 2, "likely": 1, "unusable": 0}
    best: dict[tuple[str, str, str], Found] = {}
    for f in found:
        k = (f.game_id, f.path, f.member)
        if k not in best or rank[f.confidence] > rank[best[k].confidence]:
            best[k] = f
    return list(best.values())


@job_handler(SCAN)
async def scan_library(ctx: JobContext) -> dict[str, Any]:
    svc = ctx.services
    roots = source_roots(svc.env)
    if not roots:
        ctx.log("No ROM library mounted at /roms (and no /import). Nothing to scan.")
        return {"found": 0}
    with ctx.session() as s:
        games = s.scalars(select(Game)).all()
        urls = set()
        for g in games:
            urls |= hash_list_urls([r for _id, r in recipes_of(svc, g)])
    for url in urls:
        await fetch_hash_list(svc.http, svc.sessionmaker, url)

    with ctx.session() as s:
        needs = build_needs(s, svc)
        cache = {r.path: r for r in s.scalars(select(ScanFile))}
        for r in cache.values():
            s.expunge(r)
    if not needs:
        ctx.log("No game in the library needs an input yet")
        return {"found": 0}

    ctx.progress(None, "Scanning the ROM library")

    def work(stop: threading.Event) -> Scanner:
        sc = Scanner(needs, cache, stop)
        for name, root in roots.items():
            ctx.log(f"Scanning /{name}")
            sc.walk(root)
        return sc

    sc: Scanner = await _in_thread(ctx, work)
    found = _dedupe(sc.found)
    ctx.log(f"{sc.seen} files looked at, {sc.hashed} hashed, {len(found)} matches")

    with ctx.session() as s:
        for key, (size, mtime, info) in sc.cache_updates.items():
            row = s.get(ScanFile, key)
            if row is None:
                s.add(ScanFile(path=key, size=size, mtime=mtime, info=info, scanned_at=utcnow()))
            else:
                row.size, row.mtime, row.info, row.scanned_at = size, mtime, info, utcnow()
        keep = {(f.game_id, f.path, f.member) for f in found}
        for c in s.scalars(select(ScanCandidate).where(ScanCandidate.state == CandidateState.new)):
            if (c.game_id, c.path, c.member) not in keep:
                s.delete(c)  # gone from the library, or no longer matches
        new_ids = []
        for f in found:
            c = s.scalar(
                select(ScanCandidate).where(
                    ScanCandidate.game_id == f.game_id,
                    ScanCandidate.path == f.path,
                    ScanCandidate.member == f.member,
                )
            )
            if c is None:
                c = ScanCandidate(
                    game_id=f.game_id,
                    path=f.path,
                    member=f.member,
                    kind=f.kind,
                    confidence=f.confidence,
                    reason=f.reason,
                    size=f.size,
                    state=CandidateState.new,
                    found_at=utcnow(),
                )
                s.add(c)
                s.flush()
                new_ids.append(c.id)
            else:
                c.kind, c.confidence, c.reason, c.size = f.kind, f.confidence, f.reason, f.size
        settings = app_settings.load(s)

    accepted = 0
    if settings.auto_accept_scan_matches:
        with ctx.session() as s:
            for c in auto_accept_choices(s):
                if already_have(s, c):
                    c.state = CandidateState.accepted
                    continue
                accept(ctx.manager, s, c, mode="reference")
                ctx.log(f"Added a match for {c.game_id} automatically ({c.reason})")
                accepted += 1
    for gid in {f.game_id for f in found}:
        svc.events.publish(events.GAME_UPDATED, {"id": gid})
    ctx.progress(1.0, f"{len(found)} matches, {accepted} added")
    return {"found": len(found), "new": len(new_ids), "auto_accepted": accepted}


def auto_accept_choices(session) -> list[ScanCandidate]:
    """Suggestions safe to take without asking.

    Every hash- or ID-verified match; and for a game that has no input yet, its
    one and only "likely" match (e.g. the single vf2.zip in the library).
    """
    new = session.scalars(
        select(ScanCandidate).where(ScanCandidate.state == CandidateState.new)
    ).all()
    out = [c for c in new if c.confidence == "verified"]
    by_game: dict[str, list[ScanCandidate]] = defaultdict(list)
    for c in new:
        if c.confidence != "unusable":
            by_game[c.game_id].append(c)
    for game_id, cands in by_game.items():
        has_input = session.scalar(select(Input.id).where(Input.game_id == game_id)) is not None
        if not has_input and len(cands) == 1 and cands[0].confidence == "likely":
            out.append(cands[0])
    return out


def already_have(session, c: ScanCandidate) -> bool:
    """An input already points at this file (or this archive entry was extracted)."""
    where = str(Path(c.path).resolve())
    for inp in session.scalars(select(Input).where(Input.game_id == c.game_id)):
        origin = (inp.facts or {}).get("origin") or {}
        if origin.get("path") == where and origin.get("member", "") == c.member:
            return True
    return False


def accept(jobs, session, c: ScanCandidate, mode: str = "reference"):
    """Queue the import for a suggestion. Archive entries are extracted into the vault."""
    params: dict[str, Any] = {
        "source": c.path,
        "mode": mode,
        "candidate_id": c.id,
        "origin": "scan",
    }
    if c.member:
        params["member"] = c.member
    return jobs.enqueue(session, IMPORT, game_id=c.game_id, params=params)
