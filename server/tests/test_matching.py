from __future__ import annotations

import json
from pathlib import Path

import pytest

from recompmanager.github import normalize_release, parse_repo
from recompmanager.platforms import fallbacks, is_platform
from recompmanager.recipes.matching import classify, guess_rules, match_assets
from recompmanager.recipes.store import RecipeStore

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def releases(fixture: str):
    data = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    return [normalize_release(r) for r in data["releases"]]


def test_platform_keys_and_fallbacks():
    assert is_platform("linux-x64-steamdeck")
    assert is_platform("macos-universal")
    assert not is_platform("windows-universal")
    assert not is_platform("amiga-68k")
    assert fallbacks("linux-x64-steamdeck") == ["linux-x64-steamdeck", "linux-x64"]
    assert fallbacks("macos-arm64") == ["macos-arm64", "macos-universal"]
    assert fallbacks("windows-x64") == ["windows-x64"]


@pytest.mark.parametrize(
    "value",
    [
        "https://github.com/mr-finster/vf2-recomp",
        "https://github.com/mr-finster/vf2-recomp.git",
        "https://github.com/mr-finster/vf2-recomp/releases/tag/v1",
        "github.com/mr-finster/vf2-recomp",
        "mr-finster/vf2-recomp",
    ],
)
def test_parse_repo(value):
    assert parse_repo(value) == "mr-finster/vf2-recomp"


@pytest.mark.parametrize(
    "name,platform",
    [
        ("Foo-1.0-win64.zip", "windows-x64"),
        ("Foo-windows-arm64.zip", "windows-arm64"),
        ("UltraTears-0.3.1-windows-x86_64.zip", "windows-x64"),
        ("Foo.AppImage", "linux-x64"),
        ("Foo-linux-aarch64.tar.gz", "linux-arm64"),
        ("Foo-deck.tar.gz", "linux-x64-steamdeck"),
        ("Foo-mac.zip", "macos-universal"),
        ("Foo-macos-arm64.zip", "macos-arm64"),
        ("SoH.dmg", "macos-universal"),
        ("Foo.apk", "android-arm64"),
        ("Foo-win32.zip", None),
        ("checksums.txt", None),
        ("Foo-1.0-source.zip", None),
        ("DOAXBV-0.6.2.zip", None),
    ],
)
def test_classify(name, platform):
    c = classify(name)
    assert (c.platform if c else None) == platform


@pytest.mark.parametrize(
    "fixture,expected",
    [
        (
            "mr-finster__vf2-recomp.json",
            {"windows-x64", "macos-universal", "linux-x64-steamdeck", "linux-arm64"},
        ),
        ("harbourmasters__shipwright.json", {"windows-x64", "macos-universal", "linux-x64"}),
        ("awakenchan__lanternengine.json", {"windows-x64", "linux-x64"}),
        ("kuraeow__ultratears.json", {"windows-x64", "linux-x64"}),
        ("norain211__doaxvb-re.json", {"linux-x64", "macos-universal"}),
    ],
)
def test_guessed_rules_hold_across_releases(fixture, expected):
    rels = releases(fixture)
    rules, _ = guess_rules(rels)
    assert set(rules) == expected
    # Rules from the newest release keep matching the previous one (versions and
    # codenames in names are not baked into the rules).
    prev = match_assets(rules, rels[1]["assets"])
    assert set(prev) <= expected and len(prev) >= len(expected) - 1


def test_builtin_recipes_match_every_listed_platform():
    store = RecipeStore(Path("unused"))
    by_repo = {}
    for p in FIXTURES.glob("*.json"):
        data = json.loads(p.read_text(encoding="utf-8"))
        by_repo[data["repo"]["full_name"].lower()] = p.name
    for recipe in store.builtins().values():
        rels = releases(by_repo[recipe.source.repo.lower()])
        newest = next(
            r for r in rels if recipe.source.channel == "prerelease" or not r["prerelease"]
        )
        assert set(match_assets(recipe.assets, newest["assets"])) == set(recipe.assets), recipe.id


def test_guessing_survives_packaging_changes_and_flatpaks():
    # Asset names as published by Zelda64Recomp v1.1.1 → v1.2.2.
    def rel(tag, names):
        return {"tag": tag, "prerelease": False, "assets": [{"name": n, "size": 1} for n in names]}

    p = "Zelda64Recompiled"
    rels = [
        rel(
            "v1.2.2",
            [
                f"{p}-v1.2.2-Linux-ARM64.zip",
                f"{p}-v1.2.2-Linux-Flatpak-X64.zip",
                f"{p}-v1.2.2-Linux-X64.zip",
                f"{p}-v1.2.2-macOS.zip",
                f"{p}-v1.2.2-Windows.zip",
            ],
        ),
        rel(
            "v1.2.1",
            [
                f"{p}-v1.2.1-Linux-ARM64.zip",
                f"{p}-v1.2.1-Linux-X64-Flatpak.zip",
                f"{p}-v1.2.1-Linux-X64.zip",
                f"{p}-v1.2.1-macOS.zip",
                f"{p}-v1.2.1-Windows.zip",
            ],
        ),
        rel(
            "v1.1.1",
            [
                f"{p}-v1.1.1-Linux-ARM64.zip",
                f"{p}-v1.1.1-Linux-x64.tar.gz",
                f"{p}-v1.1.1-Windows-x64.zip",
            ],
        ),
    ]
    rules, unmatched = guess_rules(rels)
    assert unmatched == [f"{p}-v1.2.2-Linux-Flatpak-X64.zip"]
    for r in rules.values():
        assert "1.2.2" not in r.match.replace("\\", "")  # no version baked in
    nxt = [{"name": n.replace("1.2.2", "1.3.0")} for n in (a["name"] for a in rels[0]["assets"])]
    assert match_assets(rules, nxt) == {
        "linux-arm64": f"{p}-v1.3.0-Linux-ARM64.zip",
        "linux-x64": f"{p}-v1.3.0-Linux-X64.zip",
        "macos-universal": f"{p}-v1.3.0-macOS.zip",
        "windows-x64": f"{p}-v1.3.0-Windows.zip",
    }
