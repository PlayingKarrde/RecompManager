"""Install plans and client registration."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from recompmanager.config import Env
from tests.conftest import wait_idle, wait_job
from tests.dumps import fake_n64_rom, ps4_folder, zip_with
from tests.fakegithub import FakeGitHub
from tests.test_auth import assert_error
from tests.test_inputs import SOH_HASHES, do_import, inputs
from tests.test_library import add, blob

VF2_ASSETS = {
    "VirtuaFighter2Recomp-windows.zip": blob("vf2-win"),
    "VirtuaFighter2Recomp-mac.zip": blob("vf2-mac"),
    "VirtuaFighter2Recomp-deck.tar.gz": blob("vf2-deck"),
    "VirtuaFighter2Recomp-linux-arm64.tar.gz": blob("vf2-arm"),
}


def plan(client, headers, recomp, platform, **params) -> dict:
    r = client.get(
        f"/api/v1/recomps/{recomp}/install-plan",
        params={"platform": platform, **params},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def steps_of(p: dict, type_: str) -> list[dict]:
    return [s for s in p["steps"] if s["type"] == type_]


@pytest.fixture
def vf2(client: TestClient, admin, github: FakeGitHub, env: Env) -> TestClient:
    """VF2 (built-in recipe) with a fake release downloaded and a fake vf2.zip input."""
    github.add_repo("mr-finster/vf2-recomp", "VF2")
    github.publish("mr-finster/vf2-recomp", "v9.9.9", VF2_ASSETS)
    add(client, admin, "mr-finster/vf2-recomp")
    wait_idle(client, admin)
    job = client.post("/api/v1/recomps/vf2-recomp/update", headers=admin).json()
    assert wait_job(client, job["id"], admin)["status"] == "succeeded"
    zip_with(env.import_dir / "vf2.zip", {"epr.bin": b"fake"})
    do_import(client, admin, "virtua-fighter-2", env.import_dir / "vf2.zip")
    return client


def test_vf2_windows_plan(vf2, admin, reader):
    p = plan(vf2, reader, "vf2-recomp", "windows-x64")
    assert p["ready"] is True and p["problems"] == []
    assert (
        p["version"] == "v9.9.9" and p["platform"] == "windows-x64" and p["strategy"] == "runtime"
    )
    assert [s["type"] for s in p["steps"]] == ["download_artifact", "write_file", "download_input"]
    art = p["steps"][0]
    assert art["name"] == "VirtuaFighter2Recomp-windows.zip" and art["extract"] is True
    assert art["sha256"] == hashlib.sha256(VF2_ASSETS[art["name"]]).hexdigest()
    assert vf2.get(art["url"], headers=reader).content == VF2_ASSETS[art["name"]]
    assert p["steps"][1] == {
        "type": "write_file",
        "path": "{install}/VirtuaFighter2/portable.txt",
        "content": "",
    }
    inp = p["steps"][2]
    assert inp["to"] == "{install}/VirtuaFighter2/roms/vf2.zip" and inp["kind"] == "file"
    assert inp["status"] == "unverified" and any("verified" in w for w in p["warnings"])
    assert p["launch"] == {"exe": "VirtuaFighter2/VirtuaFighter2.exe", "args": [], "env": {}}
    assert p["saves"] == {"path": "{install}/VirtuaFighter2"}


def test_platform_fallback(vf2, reader):
    deck = plan(vf2, reader, "vf2-recomp", "linux-x64-steamdeck")
    assert deck["platform"] == "linux-x64-steamdeck"
    assert steps_of(deck, "download_artifact")[0]["name"] == "VirtuaFighter2Recomp-deck.tar.gz"
    assert deck["launch"]["exe"] == "VirtuaFighter2/VirtuaFighter2.sh"  # from linux-x64
    mac = plan(vf2, reader, "vf2-recomp", "macos-arm64")
    assert mac["platform"] == "macos-universal"
    assert steps_of(mac, "download_artifact")[0]["name"] == "VirtuaFighter2Recomp-mac.zip"
    win_arm = plan(vf2, reader, "vf2-recomp", "windows-arm64")
    assert (
        win_arm["ready"] is False and "no release file for windows-arm64" in win_arm["problems"][0]
    )
    assert_error(
        vf2.get(
            "/api/v1/recomps/vf2-recomp/install-plan", params={"platform": "amiga"}, headers=reader
        ),
        422,
        "invalid_platform",
    )


def test_plan_needs_input_and_download(client, admin, github: FakeGitHub):
    github.add_repo("mr-finster/vf2-recomp", "VF2")
    github.publish("mr-finster/vf2-recomp", "v9.9.9", VF2_ASSETS)
    add(client, admin, "mr-finster/vf2-recomp")
    wait_idle(client, admin)
    p = plan(client, admin, "vf2-recomp", "windows-x64")
    assert p["ready"] is False
    assert p["problems"][0] == "Nothing is downloaded on the server yet"
    assert p["problems"][1].startswith("Add the game input")  # both are reported
    wait_job(
        client, client.post("/api/v1/recomps/vf2-recomp/update", headers=admin).json()["id"], admin
    )
    p = plan(client, admin, "vf2-recomp", "windows-x64")
    assert p["ready"] is False
    assert p["problems"] == [
        "Add the game input on the server: "
        "MAME `vf2` set (Virtua Fighter 2, Version 2.1), as vf2.zip"
    ]
    assert not steps_of(p, "download_input")


def test_soh_slots(client, admin, github: FakeGitHub, env: Env):
    github.add_repo("HarbourMasters/Shipwright", "SoH")
    github.publish("HarbourMasters/Shipwright", "9.9.9", {"SoH-Test-Win64.zip": blob("soh")})
    rom = fake_n64_rom("pal10")
    github.blobs[SOH_HASHES] = json.dumps(
        [{"name": "PAL 1.0", "sha1": hashlib.sha1(rom).hexdigest()}]
    ).encode()
    add(client, admin, "HarbourMasters/Shipwright")
    wait_idle(client, admin)
    wait_job(
        client, client.post("/api/v1/recomps/shipwright/update", headers=admin).json()["id"], admin
    )
    env.import_dir.mkdir(parents=True)
    (env.import_dir / "oot.z64").write_bytes(rom)
    do_import(client, admin, "ocarina-of-time", env.import_dir / "oot.z64")
    p = plan(client, admin, "shipwright", "windows-x64")
    assert p["ready"] is True
    [inp] = steps_of(p, "download_input")
    assert (inp["slot"], inp["status"], inp["to"]) == ("oot", "verified", "{install}/oot.z64")
    # No Docker here, so the extraction falls back to placing the ROM; MQ is optional.
    assert all("extracted the game data" in w for w in p["warnings"]) and p["warnings"]
    linux = plan(client, admin, "shipwright", "linux-x64")
    assert linux["ready"] is False  # no Linux asset in this fake release
    mac = plan(client, admin, "shipwright", "macos-universal")
    assert mac["strategy"] == "manual"


def test_folder_input_step(client, admin, github: FakeGitHub, env: Env):
    github.add_repo("Awakenchan/LanternEngine", "Lantern")
    github.publish(
        "Awakenchan/LanternEngine",
        "v1.0-beta",
        {"LanternEngine-v1.0-beta-win64.zip": blob("le")},
        prerelease=True,
    )
    add(client, admin, "Awakenchan/LanternEngine")
    wait_idle(client, admin)
    wait_job(
        client,
        client.post("/api/v1/recomps/lantern-engine/update", headers=admin).json()["id"],
        admin,
    )
    do_import(
        client, admin, "bloodborne", ps4_folder(env.import_dir / "CUSA03173"), mode="reference"
    )
    p = plan(client, admin, "lantern-engine", "windows-x64")
    assert p["ready"] is True
    [inp] = steps_of(p, "download_input")
    assert inp["kind"] == "folder" and inp["status"] == "verified"
    assert inp["manifest_url"].endswith("/manifest") and inp["sha256"] is None
    assert steps_of(p, "download_artifact")[0]["strip_root"] is True
    assert steps_of(p, "instructions")


def test_server_build_falls_back_to_manual(client, admin, github: FakeGitHub, env: Env):
    github.add_repo("someone/builds", "A recomp of Build Game")
    github.publish("someone/builds", "v1", {"Builds-windows.zip": blob("b")})
    r = add(client, admin, "someone/builds")
    recipe = r["recipe"]
    recipe["input"] = {"kind": "file", "description": "The disc"}
    recipe["platforms"] = {"default": {"strategy": "server_build", "notes": "Build it yourself."}}
    client.patch(
        "/api/v1/recomps/builds", json={"recipe": recipe, "status": "active"}, headers=admin
    )
    wait_idle(client, admin)
    wait_job(
        client, client.post("/api/v1/recomps/builds/update", headers=admin).json()["id"], admin
    )
    env.import_dir.mkdir(parents=True)
    (env.import_dir / "disc.bin").write_bytes(b"disc")
    do_import(client, admin, "build-game", env.import_dir / "disc.bin")
    p = plan(client, admin, "builds", "windows-x64")
    assert p["strategy"] == "manual" and p["ready"] is True
    assert [s["type"] for s in p["steps"]] == [
        "download_artifact",
        "download_input",
        "instructions",
    ]
    assert p["steps"][1]["to"] == "{data}/builds/input"
    assert "hasn't built this platform yet" in p["warnings"][-1]


def test_clients(client, admin, reader, github: FakeGitHub, env: Env):
    r = client.post(
        "/api/v1/clients/register",
        json={"name": "Deck", "platform": "linux-x64-steamdeck"},
        headers=reader,
    )
    c = r.json()
    again = client.post(
        "/api/v1/clients/register",
        json={"name": "Deck", "platform": "linux-x64-steamdeck"},
        headers=reader,
    )
    assert again.json()["id"] == c["id"]
    assert_error(
        client.post(
            "/api/v1/clients/register", json={"name": "X", "platform": "toaster"}, headers=reader
        ),
        422,
        "invalid_platform",
    )
    github.add_repo("mr-finster/vf2-recomp", "VF2")
    github.publish("mr-finster/vf2-recomp", "v1", VF2_ASSETS)
    add(client, admin, "mr-finster/vf2-recomp")
    wait_idle(client, admin)
    wait_job(
        client, client.post("/api/v1/recomps/vf2-recomp/update", headers=admin).json()["id"], admin
    )
    r = client.put(
        f"/api/v1/clients/{c['id']}/installed",
        json={"installed": {"vf2-recomp": "v0"}},
        headers=reader,
    )
    assert r.json()["out_of_date"] == ["vf2-recomp"]
    r = client.put(
        f"/api/v1/clients/{c['id']}/installed",
        json={"installed": {"vf2-recomp": "v1"}},
        headers=reader,
    )
    assert r.json()["out_of_date"] == []
    listed = client.get("/api/v1/clients", headers=admin).json()
    assert [x["name"] for x in listed] == ["Deck"]
    assert_error(client.get("/api/v1/clients", headers=reader), 403, "forbidden")
    other = client.post(
        "/api/v1/tokens", json={"name": "pc", "scope": "read"}, headers=admin
    ).json()
    assert_error(
        client.put(
            f"/api/v1/clients/{c['id']}/installed",
            json={"installed": {}},
            headers={"Authorization": f"Bearer {other['token']}"},
        ),
        404,
        "not_found",
    )
    assert inputs(client, admin, "virtua-fighter-2") == []
