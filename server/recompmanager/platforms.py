"""Platform keys and fallback.

Keys look like ``linux-x64`` or ``linux-x64-steamdeck``: an OS, an architecture and
an optional variant. Clients ask for their most specific key; ``fallbacks`` gives
the order the server tries.
"""

from __future__ import annotations

import re

OSES = ("windows", "linux", "macos", "android")
ARCHES = ("x64", "arm64", "universal")
PLATFORM_RE = re.compile(r"^(windows|linux|macos|android)-(x64|arm64|universal)(-[a-z0-9]+)?$")

KNOWN = [
    "windows-x64",
    "windows-arm64",
    "linux-x64",
    "linux-arm64",
    "linux-x64-steamdeck",
    "macos-x64",
    "macos-arm64",
    "macos-universal",
    "android-arm64",
]


def is_platform(key: str) -> bool:
    m = PLATFORM_RE.match(key)
    if not m:
        return False
    os_, arch = m.group(1), m.group(2)
    return arch != "universal" or os_ == "macos"


def fallbacks(key: str) -> list[str]:
    """Keys to try for a client, most specific first.

    ``linux-x64-steamdeck`` → ``linux-x64``; ``macos-arm64`` → ``macos-universal``.
    """
    m = PLATFORM_RE.match(key)
    if not m:
        return [key]
    os_, arch, variant = m.group(1), m.group(2), m.group(3)
    out = [key]
    if variant:
        out.append(f"{os_}-{arch}")
    if os_ == "macos" and arch in ("x64", "arm64"):
        out.append("macos-universal")
    return out
