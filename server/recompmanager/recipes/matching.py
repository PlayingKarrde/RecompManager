"""Match release assets to platform keys, and guess rules for a draft recipe."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from recompmanager.recipes.schema import AssetRule


def match_assets(rules: dict[str, AssetRule], assets: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Platform key → asset name, for the first asset each rule matches.

    One asset may serve several platforms (e.g. doaxvb's source zip).
    """
    out: dict[str, str] = {}
    for platform, rule in rules.items():
        rx = re.compile(rule.match)
        for a in assets:
            if rx.search(a["name"]):
                out[platform] = a["name"]
                break
    return out


def platforms_by_asset(rules: dict[str, AssetRule], assets: Sequence[dict[str, Any]]):
    """Asset name → sorted platform keys it serves."""
    by_asset: dict[str, list[str]] = {}
    for platform, name in match_assets(rules, assets).items():
        by_asset.setdefault(name, []).append(platform)
    return {k: sorted(v) for k, v in by_asset.items()}


# ---------------------------------------------------------------- guessing

_IGNORE = re.compile(
    r"(\.(sha\d*|md5|sig|asc|txt|json|pdb|sym|dbg|blockmap|yml|yaml)$)"
    r"|checksum|(^|[-_.])sources?([-_.]|$)|symbols",
    re.I,
)
_SEP = "-_. "
_ARCHIVE_RANK = [
    (re.compile(r"\.(zip|7z|tar\.(gz|xz|bz2|zst)|tgz)$", re.I), 3),
    (re.compile(r"\.(appimage|dmg|apk|flatpak)$", re.I), 2),
    (re.compile(r"\.(exe|msi|deb|rpm)$", re.I), 1),
]


@dataclass(frozen=True)
class Classified:
    platform: str
    hint: str  # the word in the name that gave the OS away, for regex building


def _tokens(name: str) -> list[str]:
    lower = re.sub(r"x86[-_]64", "x64", name.lower())
    return [t for t in re.split(r"[-_. ]+", lower) if t]


def _ext(name: str) -> str:
    m = re.search(r"(\.tar)?\.[A-Za-z0-9]+$", name)
    return m.group(0).lower() if m else ""


def classify(name: str) -> Classified | None:
    """Guess the platform key of a release asset from its file name."""
    if _IGNORE.search(name):
        return None
    lower = name.lower()
    toks = _tokens(name)
    tokset = set(toks)

    def has(*words: str) -> str | None:
        return next((w for w in words if w in tokset), None)

    arm = has("arm64", "aarch64", "armv8")
    x64 = has("x64", "x86_64", "amd64", "win64", "64bit")
    if has("win32", "i386", "i686", "x86") and not x64 and not arm:
        return None  # 32-bit builds aren't supported

    if lower.endswith(".apk") or (hint := has("android")):
        return Classified("android-arm64", "android" if not lower.endswith(".apk") else ".apk")
    if hint := has("deck", "steamdeck", "steamos"):
        return Classified("linux-x64-steamdeck", hint)
    if lower.endswith(".dmg") or has("mac", "macos", "osx", "darwin", "apple", "universal"):
        hint = has("mac", "macos", "osx", "darwin", "apple", "universal") or ".dmg"
        arch = "arm64" if arm else "x64" if has("x64", "x86_64", "intel", "amd64") else "universal"
        return Classified(f"macos-{arch}", hint)
    if lower.endswith((".exe", ".msi")) or has("win", "win64", "windows", "msvc", "mingw"):
        hint = has("windows", "win64", "win", "msvc", "mingw") or lower.rsplit(".", 1)[-1]
        return Classified("windows-arm64" if arm else "windows-x64", hint)
    if lower.endswith((".appimage", ".deb", ".rpm", ".flatpak")) or has(
        "linux", "appimage", "ubuntu", "debian", "fedora", "flatpak"
    ):
        hint = has("linux", "appimage", "ubuntu", "debian", "fedora", "flatpak") or "appimage"
        return Classified("linux-arm64" if arm else "linux-x64", hint)
    return None


_SECONDARY = {"flatpak", "snap", "debug", "dbg", "symbols", "pdb"}


def _rank(asset: dict[str, Any]) -> tuple[int, int, int]:
    """Higher is a better pick among one platform's assets: plain builds over
    Flatpak/debug variants, archives over bare executables, then larger."""
    secondary = -len(_SECONDARY & set(_tokens(asset["name"])))
    kind = next((r for rx, r in _ARCHIVE_RANK if rx.search(asset["name"])), 0)
    return secondary, kind, int(asset.get("size") or 0)


def _common_suffix(names: Sequence[str]) -> str:
    """Longest common suffix, ignoring case (rules are case-insensitive)."""
    rev = [n.lower()[::-1] for n in names]
    n = 0
    for chars in zip(*rev, strict=False):
        if len(set(chars)) != 1:
            break
        n += 1
    return names[0][len(names[0]) - n :] if n else ""


def _suffix_rule(name: str, hint: str, latest: Sequence[str], history: Sequence[str]) -> str | None:
    """Shortest name suffix (starting at a separator) that picks exactly this asset
    and is shared by every name in ``history``; None if there isn't one."""
    common = _common_suffix([name, *history]) if history else name
    starts = [i for i, ch in enumerate(name) if ch in _SEP and len(name) - i <= len(common)]
    candidates = [name[i:] for i in sorted(starts, reverse=True)]

    def unique(suffix: str) -> bool:
        rx = re.compile(re.escape(suffix) + "$", re.I)
        return sum(1 for n in latest if rx.search(n)) == 1

    ok = [s for s in candidates if unique(s)]
    with_hint = [s for s in ok if hint.lower() in s.lower()]
    for pick in (with_hint, ok):
        if pick:
            return "(?i)" + re.escape(pick[0]) + "$"
    return None


def _rule_for(name: str, hint: str, latest: Sequence[str], history: Sequence[str], tag: str) -> str:
    """A rule for ``name`` that keeps working for future releases.

    ``history`` holds the same platform's most similar asset name in each older
    release, newest first. Using as much history as still yields a rule lets
    version numbers and codenames drop out; packaging changes in old releases
    (``-Windows-x64.zip`` → ``-Windows.zip``) just shorten the history used.
    """
    for k in range(len(history), -1, -1):
        rule = _suffix_rule(name, hint, latest, history[:k])
        if rule:
            return rule
    # Last resort: the exact name with the version number wildcarded.
    pattern = re.escape(name)
    for v in sorted({tag, tag.lstrip("vV")} - {""}, key=len, reverse=True):
        pattern = pattern.replace(re.escape(v), ".+")
    return "(?i)^" + pattern + "$"


def guess_rules(releases: Sequence[dict[str, Any]]) -> tuple[dict[str, AssetRule], list[str]]:
    """Guess asset rules from releases (newest first).

    Returns (rules, unmatched asset names of the newest release).
    """
    if not releases:
        return {}, []
    latest_assets = releases[0]["assets"]
    latest_names = [a["name"] for a in latest_assets]

    picked: dict[str, tuple[dict[str, Any], Classified]] = {}
    for a in latest_assets:
        c = classify(a["name"])
        if c is None:
            continue
        cur = picked.get(c.platform)
        if cur is None or _rank(a) > _rank(cur[0]):
            picked[c.platform] = (a, c)

    rules: dict[str, AssetRule] = {}
    for platform, (asset, c) in sorted(picked.items()):
        # The counterpart of this asset in each older release: same platform,
        # same packaging, and the most similar name (so a Flatpak next to a plain
        # build doesn't stand in for it).
        name = asset["name"]
        history = []
        for rel in releases[1:4]:
            same = [
                x["name"]
                for x in rel["assets"]
                if (cc := classify(x["name"]))
                and cc.platform == platform
                and _ext(x["name"]) == _ext(name)
            ]
            if same:
                history.append(max(same, key=lambda n: len(_common_suffix([n, name]))))
        rule = _rule_for(name, c.hint, latest_names, history, releases[0].get("tag", ""))
        rules[platform] = AssetRule(match=rule)

    used = {a["name"] for a, _ in picked.values()}
    unmatched = [n for n in latest_names if n not in used]
    return rules, unmatched
