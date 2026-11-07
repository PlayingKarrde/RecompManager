"""ROM library scan over a synthetic library. No real game data."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from recompmanager.config import Env
from tests.conftest import wait_idle, wait_job
from tests.dumps import fake_n64_rom, make_ps4_pkg, ps4_folder, switch_folder, zip_with
from tests.fakegithub import FakeGitHub
from tests.test_auth import assert_error
from tests.test_inputs import SOH_HASHES, fit, inputs
from tests.test_library import BUILTIN_REPOS, add


def sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()  # noqa: S324


@pytest.fixture
def library(client: TestClient, admin, github: FakeGitHub, env: Env) -> TestClient:
    github.load_all_fixtures()
    oot, mq = fake_n64_rom("oot-pal10"), fake_n64_rom("oot-mq")
    github.blobs[SOH_HASHES] = json.dumps(
        [{"name": "PAL 1.0", "sha1": sha1(oot)}, {"name": "PAL MQ", "sha1": sha1(mq)}]
    ).encode()
    for url in BUILTIN_REPOS.values():
        add(client, admin, url)
    wait_idle(client, admin)

    roms = env.roms_dir
    zip_with(  # zipped, byte-swapped, with a readme beside it
        roms / "Nintendo 64/Legend of Zelda, The - Ocarina of Time (Europe).zip",
        {
            "Ocarina of Time (Europe).v64": fake_n64_rom("oot-pal10", order="v64"),
            "readme.txt": b"x",
        },
    )
    (roms / "Nintendo 64/loose").mkdir(parents=True)
    (roms / "Nintendo 64/loose/oot mq.z64").write_bytes(mq)
    (roms / "Nintendo 64/loose/some other game.z64").write_bytes(fake_n64_rom("other"))
    zip_with(roms / "Arcade/mame/vf2.zip", {"epr-17567.bin": b"not real"})
    zip_with(roms / "Arcade/mame/vf2a.zip", {"x.bin": b"other set"})
    ps4_folder(roms / "PS4/Bloodborne/CUSA03173")
    (roms / "PS4/Bloodborne.pkg").write_bytes(make_ps4_pkg("UP9000-CUSA03173_00-BLOODBORNE000000"))
    switch_folder(roms / "Switch/TotK dump", files=40)
    (roms / "Switch/Zelda TotK [0100F2C0115B6000][v0].nsp").write_bytes(b"PFS0 fake")
    iso = bytearray(0x10000 + 64)
    iso[0x10000 : 0x10000 + 20] = b"MICROSOFT*XBOX*MEDIA"
    (roms / "Xbox").mkdir()
    (roms / "Xbox/Dead or Alive Xtreme Beach Volleyball (USA).iso").write_bytes(bytes(iso))
    return client


def scan(client, admin) -> dict:
    job = client.post("/api/v1/system/scan", headers=admin).json()
    done = wait_job(client, job["id"], admin)
    assert done["status"] == "succeeded", done
    wait_idle(client, admin)  # auto-accepted imports
    return done


def suggestions(client, admin, game, include_handled=True):
    return client.get(
        f"/api/v1/games/{game}/suggestions",
        params={"include_handled": include_handled},
        headers=admin,
    ).json()


def test_scan_finds_and_adds_inputs(library, admin, env: Env):
    done = scan(library, admin)
    # The matched Switch dump isn't walked: 40 romfs files never looked at.
    assert int(done["log"].split(" files looked at")[0].split()[-1]) < 20

    # OoT: the zipped v64 ROM is verified by hash and extracted into the vault...
    oot = {i["name"]: i for i in inputs(library, admin, "ocarina-of-time")}
    assert fit(oot["Ocarina of Time (Europe).v64"], "shipwright")["status"] == "verified"
    assert oot["Ocarina of Time (Europe).v64"]["storage"] == "vault"
    # ...and the loose MQ ROM is used where it is.
    mq = oot["oot mq.z64"]
    assert mq["storage"] == "reference" and fit(mq, "shipwright")["slot"] == "oot-mq"
    assert "some other game.z64" not in oot

    # VF2: only one vf2.zip in the library and no hashes to check → added, unverified.
    [vf2] = inputs(library, admin, "virtua-fighter-2")
    assert vf2["name"] == "vf2.zip" and vf2["storage"] == "reference"
    assert fit(vf2, "vf2-recomp")["status"] == "unverified"

    # PS4 and Switch dump folders, by title/program ID.
    [bb] = inputs(library, admin, "bloodborne")
    assert bb["kind"] == "folder" and fit(bb, "lantern-engine")["status"] == "verified"
    [totk] = inputs(library, admin, "tears-of-the-kingdom")
    assert fit(totk, "ultratears")["status"] == "verified"

    # Encrypted packages are reported, not used.
    pkg = [s for s in suggestions(library, admin, "bloodborne") if s["kind"] == "package"]
    assert pkg and pkg[0]["confidence"] == "unusable" and "encrypted" in pkg[0]["reason"]
    nsp = [s for s in suggestions(library, admin, "tears-of-the-kingdom") if s["kind"] == "package"]
    assert nsp and nsp[0]["state"] == "new"
    assert_error(
        library.post(f"/api/v1/suggestions/{nsp[0]['id']}/accept", headers=admin), 409, "unusable"
    )

    # The Xbox ISO is named like the game: the one likely match, so it's taken.
    [doa] = inputs(library, admin, "dead-or-alive-xtreme-beach-volleyball")
    assert doa["facts"]["xbox_iso"] is True


def test_rescan_uses_cache_and_adds_nothing_twice(library, admin):
    scan(library, admin)
    before = {g: len(inputs(library, admin, g)) for g in ("ocarina-of-time", "bloodborne")}
    done = scan(library, admin)
    assert " 0 hashed" in done["log"]
    after = {g: len(inputs(library, admin, g)) for g in ("ocarina-of-time", "bloodborne")}
    assert before == after


def test_manual_mode_and_dismiss(library, admin, env: Env):
    library.patch(
        "/api/v1/system/settings", json={"auto_accept_scan_matches": False}, headers=admin
    )
    scan(library, admin)
    assert inputs(library, admin, "ocarina-of-time") == []
    found = suggestions(library, admin, "ocarina-of-time", include_handled=False)
    assert {s["confidence"] for s in found} == {"verified"}
    zipped = next(s for s in found if s["member"])
    assert zipped["kind"] == "archive_member" and zipped["reason"] == "Matches PAL 1.0"

    job = library.post(f"/api/v1/suggestions/{zipped['id']}/accept", headers=admin).json()
    assert wait_job(library, job["id"], admin)["status"] == "succeeded"
    loose = next(s for s in found if not s["member"])
    assert (
        library.post(f"/api/v1/suggestions/{loose['id']}/dismiss", headers=admin).status_code == 204
    )

    scan(library, admin)
    states = {s["id"]: s["state"] for s in suggestions(library, admin, "ocarina-of-time")}
    assert states[zipped["id"]] == "accepted" and states[loose["id"]] == "dismissed"
    assert len(inputs(library, admin, "ocarina-of-time")) == 1


def test_vanished_files_drop_out_of_suggestions(library, admin, env: Env):
    library.patch(
        "/api/v1/system/settings", json={"auto_accept_scan_matches": False}, headers=admin
    )
    scan(library, admin)
    assert suggestions(library, admin, "virtua-fighter-2", include_handled=False)
    (env.roms_dir / "Arcade/mame/vf2.zip").unlink()
    scan(library, admin)
    assert suggestions(library, admin, "virtua-fighter-2", include_handled=False) == []


def test_scan_without_library(client, admin):
    job = client.post("/api/v1/system/scan", headers=admin).json()
    done = wait_job(client, job["id"], admin)
    assert done["status"] == "succeeded" and "Nothing to scan" in done["log"]
