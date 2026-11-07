"""Carry out an install plan. Every step is safe to run twice."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from rlclient.api import Api, file_sha256

STATE_FILE = ".rlclient.json"


class InstallError(Exception):
    pass


@dataclass
class Places:
    install: Path
    data: Path
    home: Path = field(default_factory=Path.home)

    def sub(self, text: str) -> str:
        return (
            text.replace("{install}", str(self.install))
            .replace("{data}", str(self.data))
            .replace("{home}", str(self.home))
        )

    def path(self, text: str) -> Path:
        return Path(self.sub(text))


def _say(msg: str) -> None:
    print(msg, flush=True)


def _progress(label: str) -> Callable[[int, int | None], None]:
    last = [-1]

    def cb(done: int, total: int | None) -> None:
        if not total:
            return
        pct = int(done * 100 / total)
        if pct != last[0] and pct % 10 == 0:
            last[0] = pct
            _say(f"  {label}: {pct}%")

    return cb


# ---------------------------------------------------------------- extraction


def _safe_target(root: Path, name: str) -> Path:
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        raise InstallError(f"Unsafe path in archive: {name}")
    return root.joinpath(*p.parts)


def _single_root(names: list[str]) -> str | None:
    tops = {PurePosixPath(n.replace("\\", "/")).parts[0] for n in names if n.strip("/")}
    if len(tops) == 1:
        top = next(iter(tops))
        if any("/" in n.replace("\\", "/").strip("/") for n in names):
            return top
    return None


def extract(archive: Path, dest: Path, strip_root: bool) -> None:
    name = archive.name.lower()
    dest.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            root = _single_root(names) if strip_root else None
            for info in z.infolist():
                rel = info.filename
                if root:
                    rel = rel.replace("\\", "/")[len(root) + 1 :]
                    if not rel:
                        continue
                target = _safe_target(dest, rel)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                mode = (info.external_attr >> 16) & 0o777
                if mode & 0o111 and sys.platform != "win32":
                    target.chmod(mode)
    elif name.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
        with tarfile.open(archive) as t:
            members = t.getmembers()
            root = _single_root([m.name for m in members]) if strip_root else None
            for m in members:
                rel = m.name
                if root:
                    rel = rel[len(root) + 1 :]
                    if not rel:
                        continue
                    if m.islnk() and m.linkname.startswith(root + "/"):
                        m.linkname = m.linkname[len(root) + 1 :]
                m.name = rel
                _safe_target(dest, rel)
                # "data" filter: refuses absolute paths, links leaving dest, devices
                t.extract(m, dest, filter="data")
    elif name.endswith(".7z"):
        exe = shutil.which("7z") or shutil.which("7zz")
        if not exe:
            raise InstallError("This needs 7-Zip (7z) installed")
        subprocess.run([exe, "x", "-y", f"-o{dest}", str(archive)], check=True)
        if strip_root:
            entries = list(dest.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                inner = entries[0]
                for child in inner.iterdir():
                    shutil.move(str(child), dest / child.name)
                inner.rmdir()
    else:
        raise InstallError(f"Don't know how to unpack {archive.name}")


# ---------------------------------------------------------------- running a plan


class Installer:
    def __init__(self, api: Api, places: Places, *, yes: bool = False, ask=input) -> None:
        self.api = api
        self.places = places
        self.yes = yes
        self.ask = ask
        self.cache = places.data / "cache"
        self.state_path = places.install / STATE_FILE
        self.state: dict[str, Any] = {}
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self) -> None:
        self.places.install.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def run(self, plan: dict[str, Any]) -> None:
        if not plan["ready"]:
            raise InstallError(
                "The server can't install this yet:\n  " + "\n  ".join(plan["problems"])
            )
        for w in plan["warnings"]:
            _say(f"Warning: {w}")
        self.places.install.mkdir(parents=True, exist_ok=True)
        for i, step in enumerate(plan["steps"], 1):
            handler = getattr(self, f"step_{step['type']}", None)
            if handler is None:
                raise InstallError(f"This client doesn't know step type {step['type']!r}")
            _say(f"[{i}/{len(plan['steps'])}] {step['type']}")
            handler(step)
        self.state["installed"] = {
            "recomp": plan["recomp_id"],
            "version": plan["version"],
            "platform": plan["platform"],
            "launch": plan["launch"],
            "saves": plan["saves"],
        }
        self._save_state()

    # ------------------------------------------------------------ steps

    def step_download_artifact(self, s: dict[str, Any]) -> None:
        cached = self.cache / f"{s['sha256'][:16]}-{s['name']}"
        self.api.download(
            s["url"], cached, sha256=s["sha256"], size=s["size"], on_progress=_progress(s["name"])
        )
        to = self.places.path(s["to"])
        key = f"artifact:{to}"
        if s["extract"]:
            if self.state.get(key) == s["sha256"] and to.exists():
                _say(f"  {s['name']}: already unpacked")
                return
            extract(cached, to, s.get("strip_root", False))
        else:
            target = to / s["name"] if to.is_dir() else to
            target.parent.mkdir(parents=True, exist_ok=True)
            if not (target.exists() and file_sha256(target) == s["sha256"]):
                shutil.copyfile(cached, target)
        self.state[key] = s["sha256"]
        self._save_state()

    def step_download_input(self, s: dict[str, Any]) -> None:
        to = self.places.path(s["to"])
        if s["kind"] == "file":
            self.api.download(
                s["url"],
                to,
                sha256=s.get("sha256"),
                size=s["size"],
                on_progress=_progress(s["name"]),
            )
            return
        manifest = self.api.get(s["manifest_url"])
        files = manifest["files"]
        done = 0
        for f in files:
            target = _safe_target(to, f["path"])
            if (
                target.exists()
                and target.stat().st_size == f["size"]
                and (self.state.get(f"input:{s['input_id']}:{f['path']}") == f["sha256"])
            ):
                done += f["size"]
                continue
            self.api.download(
                s["files_url"] + f["path"], target, sha256=f["sha256"], size=f["size"]
            )
            self.state[f"input:{s['input_id']}:{f['path']}"] = f["sha256"]
            done += f["size"]
        self._save_state()
        _say(f"  {s['name']}: {len(files)} files")

    def step_write_file(self, s: dict[str, Any]) -> None:
        p = self.places.path(s["path"])
        content = self.places.sub(s["content"])
        if p.exists() and p.read_text(encoding="utf-8") == content:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def step_chmod(self, s: dict[str, Any]) -> None:
        if sys.platform == "win32":
            return
        p = self.places.path(s["path"])
        p.chmod(int(s["mode"], 8))

    def step_run(self, s: dict[str, Any]) -> None:
        cmd = self.places.sub(s["command"])
        cwd = self.places.path(s["cwd"])
        _say(f"  The recipe wants to run:\n    {cmd}\n  in {cwd}")
        if not self.yes and self.ask("  Run it? [y/N] ").strip().lower() not in ("y", "yes"):
            raise InstallError("Stopped: the command wasn't run")
        subprocess.run(cmd, shell=True, cwd=cwd, check=True)

    def step_instructions(self, s: dict[str, Any]) -> None:
        _say("\n" + self.places.sub(s["markdown"]) + "\n")


def launch(places: Places, info: dict[str, Any]) -> subprocess.Popen:
    exe = places.sub(info["exe"])
    path = Path(exe) if Path(exe).is_absolute() else places.install / exe
    args = [places.sub(a) for a in info.get("args", [])]
    env = {**os.environ, **{k: places.sub(v) for k, v in info.get("env", {}).items()}}
    if sys.platform == "darwin" and path.suffix == ".app":
        return subprocess.Popen(["open", "-a", str(path), "--args", *args], env=env)
    if sys.platform != "win32":
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return subprocess.Popen([str(path), *args], cwd=path.parent, env=env)
