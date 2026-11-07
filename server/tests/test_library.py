"""Phase 1: track, download, serve. Uses the fake GitHub; no real game files."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from recompmanager.config import Env
from tests.conftest import wait_idle, wait_job
from tests.fakegithub import FakeGitHub
from tests.test_auth import assert_error

BUILTIN_REPOS = {
    "vf2-recomp": "https://github.com/mr-finster/vf2-recomp",
    "shipwright": "https://github.com/HarbourMasters/Shipwright",
    "lantern-engine": "https://github.com/Awakenchan/LanternEngine",
    "doaxvb-re": "https://github.com/NoRain211/doaxvb-re",
    "ultratears": "https://github.com/kuraeow/ultratears",
}


def add(client: TestClient, admin, repo_url: str) -> dict:
    r = client.post("/api/v1/games", json={"repo_url": repo_url}, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()


def blob(name: str, size: int = 50_000) -> bytes:
    """Deterministic fake asset bytes."""
    seed = hashlib.sha256(name.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


@pytest.fixture
def dummy(github: FakeGitHub) -> FakeGitHub:
    """A repo with no built-in recipe: someone/cool-recomp."""
    github.add_repo("someone/cool-recomp", "A cool recomp")
    github.publish(
        "someone/cool-recomp",
        "v1.0.0",
        {
            "CoolRecomp-1.0.0-windows-x64.zip": blob("w1"),
            "CoolRecomp-1.0.0-linux-x64.tar.gz": blob("l1", 70_000),
            "checksums.txt": b"x",
        },
    )
    return github


def active_dummy(client, admin) -> dict:
    game = add(client, admin, "someone/cool-recomp")
    r = client.patch(f"/api/v1/games/{game['id']}", json={"status": "active"}, headers=admin)
    assert r.status_code == 200
    wait_idle(client, admin)
    return r.json()


def download(client, admin, game_id: str, **body) -> dict:
    r = client.post(f"/api/v1/games/{game_id}/update", json=body or None, headers=admin)
    assert r.status_code == 202, r.text
    return wait_job(client, r.json()["id"], admin)


# ---------------------------------------------------------------- adding games


def test_add_all_five_test_games(client: TestClient, admin, github: FakeGitHub):
    github.load_all_fixtures()
    for game_id, url in BUILTIN_REPOS.items():
        g = add(client, admin, url)
        assert g["id"] == game_id
        assert g["status"] == "active"  # built-in recipes need no confirmation
    wait_idle(client, admin)

    games = {g["id"]: g for g in client.get("/api/v1/games", headers=admin).json()}
    assert set(games) == set(BUILTIN_REPOS)
    for g in games.values():
        assert g["last_check_error"] is None, g
        assert g["latest_version"], g
        assert g["update_available"] is True  # nothing downloaded yet
        assert g["current_version"] is None  # adding doesn't download by itself

    # Asset rules sort each release into the right platforms.
    detail = client.get("/api/v1/games/doaxvb-re", headers=admin).json()
    assert detail["latest_assets"] == {
        "windows-x64": "DOAXBV-0.6.2.zip",
        "linux-x64": "DOAXBV-0.6.2-Linux.zip",
        "macos-universal": "DOAXBV-0.6.2-macOS.zip",
    }
    lantern = client.get("/api/v1/games/lantern-engine", headers=admin).json()
    assert lantern["latest_version"] == "v0.9.39-beta.31"  # pre-release channel
    assert set(lantern["latest_assets"]) == {"windows-x64", "linux-x64"}
    soh = client.get("/api/v1/games/shipwright", headers=admin).json()
    assert soh["latest_assets"]["linux-x64"] == "SoH-Dewey-Bravo.appimage"
    versions = client.get("/api/v1/games/shipwright/versions", headers=admin).json()
    old = next(v for v in versions if v["tag"] == "9.2.3")
    assert "linux-x64" in old["platforms"]  # older -Linux.zip still matched
    assert old["notes"]  # release notes stored


def test_add_same_repo_twice_conflicts(client, admin, github):
    github.load_all_fixtures()
    add(client, admin, BUILTIN_REPOS["vf2-recomp"])
    r = client.post("/api/v1/games", json={"repo_url": "mr-finster/VF2-RECOMP"}, headers=admin)
    assert_error(r, 409, "conflict")


def test_add_by_builtin_recipe_id(client, admin, github):
    github.load_all_fixtures()
    assert {r["id"] for r in client.get("/api/v1/recipes", headers=admin).json()} == set(
        BUILTIN_REPOS
    )
    r = client.post("/api/v1/games", json={"recipe_id": "ultratears"}, headers=admin)
    assert r.status_code == 201 and r.json()["status"] == "active"


def test_bad_repo_urls(client, admin, github):
    r = client.post("/api/v1/games", json={"repo_url": "https://gitlab.com/a/b"}, headers=admin)
    assert_error(r, 422, "invalid_repo")
    r = client.post("/api/v1/games", json={"repo_url": "nobody/nothing"}, headers=admin)
    assert_error(r, 502, "source_error")
    r = client.post("/api/v1/games", json={}, headers=admin)
    assert_error(r, 422, "validation_error")


def test_draft_from_unknown_repo(client, admin, dummy):
    g = add(client, admin, "https://github.com/someone/cool-recomp.git")
    assert g["id"] == "cool-recomp"
    assert g["status"] == "draft"
    assert g["recipe"]["description"] == "A cool recomp"
    assert set(g["platforms"]) == {"windows-x64", "linux-x64"}
    assert g["unmatched_assets"] == ["checksums.txt"]
    assert "assets" in g["recipe"]["unverified"]
    assert g["latest_version"] == "v1.0.0"

    # Drafts can't download until confirmed.
    r = client.post("/api/v1/games/cool-recomp/update", headers=admin)
    assert_error(r, 409, "draft")

    # Edit a rule, then confirm.
    r = client.patch(
        "/api/v1/games/cool-recomp",
        json={"assets": {"windows-x64": {"match": r"-windows-x64\.zip$"}}, "name": "Cool"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["platforms"] == ["windows-x64"] and r.json()["name"] == "Cool"
    r = client.patch("/api/v1/games/cool-recomp", json={"status": "active"}, headers=admin)
    assert r.json()["status"] == "active"


def test_recipe_validation(client, admin, dummy):
    add(client, admin, "someone/cool-recomp")
    r = client.patch(
        "/api/v1/games/cool-recomp", json={"assets": {"windows-x64": {"match": "("}}}, headers=admin
    )
    assert_error(r, 422, "validation_error")
    r = client.patch(
        "/api/v1/games/cool-recomp", json={"assets": {"amiga-68k": {"match": "x"}}}, headers=admin
    )
    assert_error(r, 422, "validation_error")


def test_recipe_file_is_written_and_reread(client, admin, dummy, env: Env):
    add(client, admin, "someone/cool-recomp")
    path = env.recipes_dir / "cool-recomp.yaml"
    text = path.read_text(encoding="utf-8")
    assert "repo: someone/cool-recomp" in text
    # Hand edits are picked up.
    path.write_text(text.replace("name: Cool recomp", "name: Hand Edited"), encoding="utf-8")
    import os
    import time

    os.utime(path, (time.time() + 5, time.time() + 5))
    assert client.get("/api/v1/games/cool-recomp", headers=admin).json()["name"] == "Hand Edited"
    # A broken file is reported, not fatal.
    path.write_text("id: [", encoding="utf-8")
    os.utime(path, (time.time() + 10, time.time() + 10))
    g = client.get("/api/v1/games/cool-recomp", headers=admin).json()
    assert g["recipe_error"] and g["recipe"] is None


# ---------------------------------------------------------------- checking


def test_check_uses_etag_and_detects_new_release(client, admin, dummy):
    active_dummy(client, admin)
    before = len(dummy.api_requests("/releases"))
    job = client.post("/api/v1/games/cool-recomp/check", headers=admin).json()
    assert wait_job(client, job["id"], admin)["status"] == "succeeded"
    reqs = dummy.api_requests("/releases")[before:]
    assert reqs and reqs[-1].headers.get("if-none-match")  # conditional request sent

    dummy.publish("someone/cool-recomp", "v1.1.0", {"CoolRecomp-1.1.0-windows-x64.zip": blob("w2")})
    job = client.post("/api/v1/games/cool-recomp/check", headers=admin).json()
    result = wait_job(client, job["id"], admin)
    assert result["result"]["new_versions"] == ["v1.1.0"]
    g = client.get("/api/v1/games/cool-recomp", headers=admin).json()
    assert g["latest_version"] == "v1.1.0" and g["update_available"]


def test_check_skips_prereleases_on_stable_channel(client, admin, dummy):
    active_dummy(client, admin)
    dummy.publish(
        "someone/cool-recomp",
        "v2.0.0-beta",
        {"CoolRecomp-2.0.0-windows-x64.zip": b"b"},
        prerelease=True,
    )
    job = client.post("/api/v1/games/cool-recomp/check", headers=admin).json()
    wait_job(client, job["id"], admin)
    assert (
        client.get("/api/v1/games/cool-recomp", headers=admin).json()["latest_version"] == "v1.0.0"
    )
    client.patch("/api/v1/games/cool-recomp", json={"channel": "prerelease"}, headers=admin)
    wait_idle(client, admin)
    g = client.get("/api/v1/games/cool-recomp", headers=admin).json()
    assert g["latest_version"] == "v2.0.0-beta"


def test_scheduled_check_auto_downloads(client, admin, dummy):
    active_dummy(client, admin)
    svc = client.app.state.services
    client.portal.call(svc.jobs.wake)  # the scheduler runs check_all on its own loop
    from recompmanager.scheduler import Scheduler

    queued = client.portal.call(Scheduler(svc).check_all)
    assert queued == 1
    wait_idle(client, admin)
    g = client.get("/api/v1/games/cool-recomp", headers=admin).json()
    assert g["current_version"] == "v1.0.0"  # auto_update downloaded it


# ---------------------------------------------------------------- downloading and serving


def test_download_verify_and_ranged_serve(client, admin, dummy, env: Env):
    active_dummy(client, admin)
    job = download(client, admin, "cool-recomp")
    assert job["status"] == "succeeded", job
    assert "verified sha256" in job["log"]

    g = client.get("/api/v1/games/cool-recomp", headers=admin).json()
    assert g["current_version"] == "v1.0.0" and not g["update_available"]
    arts = client.get("/api/v1/games/cool-recomp/versions/v1.0.0/artifacts", headers=admin).json()
    assert {a["name"]: a["platforms"] for a in arts} == {
        "CoolRecomp-1.0.0-windows-x64.zip": ["windows-x64"],
        "CoolRecomp-1.0.0-linux-x64.tar.gz": ["linux-x64"],
    }
    win = next(a for a in arts if "windows" in a["name"])
    data = blob("w1")
    assert win["sha256"] == hashlib.sha256(data).hexdigest()
    assert (env.data_dir / "library/cool-recomp/v1.0.0/CoolRecomp-1.0.0-windows-x64.zip").is_file()

    url = f"/api/v1/artifacts/{win['id']}/download"
    full = client.get(url, headers=admin)
    assert full.status_code == 200 and full.content == data
    assert full.headers["accept-ranges"] == "bytes"
    part = client.get(url, headers={**admin, "Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == data[100:200]
    # Resuming a client download: open-ended range.
    tail = client.get(url, headers={**admin, "Range": f"bytes={len(data) - 10}-"})
    assert tail.status_code == 206 and tail.content == data[-10:]


def test_read_token_can_download_but_not_change(client, admin, reader, dummy):
    active_dummy(client, admin)
    download(client, admin, "cool-recomp")
    arts = client.get("/api/v1/games/cool-recomp/versions/v1.0.0/artifacts", headers=reader).json()
    assert (
        client.get(f"/api/v1/artifacts/{arts[0]['id']}/download", headers=reader).status_code == 200
    )
    assert_error(client.post("/api/v1/games/cool-recomp/check", headers=reader), 403, "forbidden")
    assert_error(client.delete("/api/v1/games/cool-recomp", headers=reader), 403, "forbidden")


def test_interrupted_download_resumes(client, admin, dummy, env: Env):
    active_dummy(client, admin)
    url = "https://github.com/someone/cool-recomp/releases/download/v1.0.0/CoolRecomp-1.0.0-linux-x64.tar.gz"
    dummy.flaky[url] = 30_000
    job = download(client, admin, "cool-recomp")
    assert job["status"] == "failed" and "resume" in job["error"]
    part = env.data_dir / "library/cool-recomp/v1.0.0/CoolRecomp-1.0.0-linux-x64.tar.gz.part"
    assert part.stat().st_size == 30_000

    job = download(client, admin, "cool-recomp")
    assert job["status"] == "succeeded", job
    ranged = [r for r in dummy.requests if str(r.url) == url and r.headers.get("range")]
    assert ranged[-1].headers["range"] == "bytes=30000-"
    final = env.data_dir / "library/cool-recomp/v1.0.0/CoolRecomp-1.0.0-linux-x64.tar.gz"
    assert final.read_bytes() == blob("l1", 70_000) and not part.exists()


def test_checksum_mismatch_fails(client, admin, github, env: Env):
    github.add_repo("someone/broken")
    github.publish(
        "someone/broken",
        "v1",
        {"Broken-windows.zip": blob("b")},
        bad_digest_for="Broken-windows.zip",
    )
    add(client, admin, "someone/broken")
    client.patch("/api/v1/games/broken", json={"status": "active"}, headers=admin)
    wait_idle(client, admin)
    job = download(client, admin, "broken")
    assert job["status"] == "failed" and "Checksum mismatch" in job["error"]
    assert not list((env.data_dir / "library/broken").rglob("*.part"))
    v = client.get("/api/v1/games/broken/versions/v1", headers=admin).json()
    assert v["state"] == "failed"


def test_not_enough_disk_space(client, admin, dummy, monkeypatch):
    active_dummy(client, admin)
    from recompmanager import storage

    monkeypatch.setattr(storage, "free_bytes", lambda _p: 10)
    job = download(client, admin, "cool-recomp")
    assert job["status"] == "failed" and "Not enough disk space" in job["error"]


def test_cancel_running_download(client, admin, dummy):
    active_dummy(client, admin)
    for name in ("CoolRecomp-1.0.0-windows-x64.zip", "CoolRecomp-1.0.0-linux-x64.tar.gz"):
        dummy.slow.add(f"https://github.com/someone/cool-recomp/releases/download/v1.0.0/{name}")
    job = client.post("/api/v1/games/cool-recomp/update", headers=admin).json()
    import time

    deadline = time.monotonic() + 10
    while client.get(f"/api/v1/jobs/{job['id']}", headers=admin).json()["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.02)
    client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=admin)
    assert wait_job(client, job["id"], admin)["status"] == "cancelled"
    v = client.get("/api/v1/games/cool-recomp/versions/v1.0.0", headers=admin).json()
    assert v["state"] == "failed"


# ---------------------------------------------------------------- versions


def test_pin_prune_and_rollback(client, admin, dummy, env: Env):
    active_dummy(client, admin)
    download(client, admin, "cool-recomp")  # v1.0.0
    client.post("/api/v1/games/cool-recomp/versions/v1.0.0/pin", headers=admin)
    for tag in ("v1.1.0", "v1.2.0", "v1.3.0"):
        dummy.publish("someone/cool-recomp", tag, {f"CoolRecomp-{tag}-windows-x64.zip": blob(tag)})
        wait_job(
            client,
            client.post("/api/v1/games/cool-recomp/check", headers=admin).json()["id"],
            admin,
        )
        assert download(client, admin, "cool-recomp")["status"] == "succeeded"

    versions = {
        v["tag"]: v for v in client.get("/api/v1/games/cool-recomp/versions", headers=admin).json()
    }
    assert versions["v1.3.0"]["current"] is True
    # keep_versions defaults to 2: the newest two stay, v1.1.0 is pruned, pinned v1.0.0 stays.
    assert versions["v1.3.0"]["state"] == "downloaded"
    assert versions["v1.2.0"]["state"] == "downloaded"
    assert versions["v1.1.0"]["state"] == "pruned"
    assert versions["v1.0.0"]["state"] == "downloaded" and versions["v1.0.0"]["pinned"]
    assert not (env.data_dir / "library/cool-recomp/v1.1.0").exists()

    # Rollback to the pinned version.
    r = client.post("/api/v1/games/cool-recomp/versions/v1.0.0/activate", headers=admin)
    assert r.status_code == 200 and r.json()["current"]
    g = client.get("/api/v1/games/cool-recomp", headers=admin).json()
    assert g["current_version"] == "v1.0.0" and g["update_available"]
    assert_error(
        client.post("/api/v1/games/cool-recomp/versions/v1.1.0/activate", headers=admin),
        409,
        "not_downloaded",
    )
    # The current version and pinned versions can't be deleted.
    assert_error(
        client.delete("/api/v1/games/cool-recomp/versions/v1.0.0", headers=admin), 409, "current"
    )
    client.post("/api/v1/games/cool-recomp/versions/v1.3.0/activate", headers=admin)
    assert_error(
        client.delete("/api/v1/games/cool-recomp/versions/v1.0.0", headers=admin), 409, "pinned"
    )
    client.delete("/api/v1/games/cool-recomp/versions/v1.0.0/pin", headers=admin)
    r = client.delete("/api/v1/games/cool-recomp/versions/v1.0.0", headers=admin)
    assert r.status_code == 200 and r.json()["state"] == "pruned"
    # Downloading an old version again doesn't make it current.
    job = download(client, admin, "cool-recomp", version="v1.0.0")
    assert job["status"] == "succeeded"
    assert (
        client.get("/api/v1/games/cool-recomp", headers=admin).json()["current_version"] == "v1.3.0"
    )


def test_delete_game(client, admin, dummy, env: Env):
    active_dummy(client, admin)
    download(client, admin, "cool-recomp")
    assert client.delete("/api/v1/games/cool-recomp", headers=admin).status_code == 204
    assert not (env.data_dir / "library/cool-recomp").exists()
    assert not (env.recipes_dir / "cool-recomp.yaml").exists()
    assert (env.recipes_dir / "cool-recomp.yaml.deleted").exists()
    assert_error(client.get("/api/v1/games/cool-recomp", headers=admin), 404, "not_found")
    # It can be added again.
    add(client, admin, "someone/cool-recomp")


def test_jobs_list_and_detail(client, admin, dummy):
    active_dummy(client, admin)
    download(client, admin, "cool-recomp")
    jobs = client.get("/api/v1/jobs?game_id=cool-recomp", headers=admin).json()
    assert [j["kind"] for j in jobs][:2] == ["download", "check"]
    assert all("log" not in j for j in jobs)
    detail = client.get(f"/api/v1/jobs/{jobs[0]['id']}", headers=admin).json()
    assert detail["progress"] == 1.0 and detail["log"]
