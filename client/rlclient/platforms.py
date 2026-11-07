"""Where this client runs: its platform key and its folders."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def _os_release() -> dict[str, str]:
    out = {}
    for p in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            for line in Path(p).read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v.strip().strip('"')
            break
        except OSError:
            continue
    return out


def detect() -> str:
    """Most specific platform key for this machine, e.g. linux-x64-steamdeck."""
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64", "armv8l") else "x64"
    if sys.platform == "win32":
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    if sys.platform.startswith("linux"):
        osr = _os_release()
        if "ANDROID_ROOT" in os.environ:
            return "android-arm64"
        if osr.get("ID") == "steamos" or osr.get("VARIANT_ID") == "steamdeck":
            return "linux-x64-steamdeck"
        return f"linux-{arch}"
    raise SystemExit(f"Unsupported OS: {sys.platform}")


def data_dir() -> Path:
    """Per-user app data folder: the {data} placeholder."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "RecompManager"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/RecompManager"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "recompmanager"


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "rlclient"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/rlclient"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "rlclient"


def default_games_dir() -> Path:
    return Path.home() / "Games"
