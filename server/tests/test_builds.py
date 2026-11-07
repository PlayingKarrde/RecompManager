"""Server-side builds and extractions. A fake Docker backend runs Python stand-ins
for the builders; one test uses real Docker when RM_TEST_DOCKER=1 (CI)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recompmanager.builder import BuildError, DockerBackend, Mount, map_host_path
from recompmanager.config import Env
from recompmanager.inputs import Stopped
from recompmanager.main import create_app
from tests.conftest import wait_idle, wait_job
from tests.dumps import fake_n64_rom
from tests.fakegithub import FakeGitHub
from tests.test_auth import assert_error
from tests.test_inputs import SOH_HASHES, do_import
from tests.test_library import add, blob


class FakeBackend:
    """Stands in for Docker: runs a Python function per builder with the same view of
    files a container would get (mount targets mapped back to real paths)."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.runs: list[dict] = []
        self.images: list[str] = []
        self.behaviour = "ok"  # ok | fail | hang
        self.started = threading.Event()

    def unavailable(self) -> str | None:
        return self.reason

    def host_path(self, path: Path) -> str:
        return str(path)

    def ensure_image(self, context, tag, on_line) -> None:
        self.images.append(tag)
        on_line(f"(fake) image {tag}")

    def run(
        self,
        image,
        command,
        mounts: list[Mount],
        environment,
        *,
        user,
        network,
        memory_gb,
        cpus,
        labels,
        on_line,
        stop,
        timeout_s,
    ) -> int:
        self.runs.append(
            {
                "image": image,
                "env": dict(environment),
                "network": network,
                "mounts": {m.target: (str(m.source), m.read_only) for m in mounts},
            }
        )
        self.started.set()

        def real(container_path: str) -> Path:
            best = max(
                (m for m in mounts if container_path.startswith(m.target)),
                key=lambda m: len(m.target),
            )
            return Path(best.source) / container_path[len(best.target) :].lstrip("/")

        on_line("fake build starting")
        if self.behaviour == "hang":
            while not stop.is_set():
                time.sleep(0.02)
            raise Stopped
        if self.behaviour == "fail":
            on_line("something broke")
            return 2
        out = real(environment["OUTPUT"])
        release = real(environment["RELEASE"]).read_bytes()
        if "INPUT_OOT" in environment:  # soh-extract stand-in
            rom = real(environment["INPUT_OOT"]).read_bytes()
            (out / "oot.o2r").write_bytes(b"O2R" + hashlib.sha256(rom + release).digest())
        else:  # a generic "build"
            (out / "game.tar").write_bytes(b"TAR" + hashlib.sha256(release).digest())
        on_line("fake build done")
        return 0


SOH = "HarbourMasters/Shipwright"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def client(env: Env, github: FakeGitHub, backend: FakeBackend):
    with TestClient(
        create_app(env, http_transport=github.transport(), builder_backend=backend)
    ) as c:
        yield c


def soh_setup(client, admin, github: FakeGitHub, env: Env, *, rom_first: bool) -> bytes:
    rom = fake_n64_rom("pal10")
    github.blobs[SOH_HASHES] = json.dumps(
        [{"name": "PAL 1.0", "sha1": hashlib.sha1(rom).hexdigest()}]
    ).encode()
    github.add_repo(SOH, "SoH")
    github.publish(
        SOH, "9.9.9", {"SoH-Test-Win64.zip": blob("win"), "SoH-Test.appimage": blob("appimg")}
    )
    add(client, admin, f"https://github.com/{SOH}")
    wait_idle(client, admin)
    env.import_dir.mkdir(parents=True, exist_ok=True)
    (env.import_dir / "oot.z64").write_bytes(rom)
    if rom_first:
        do_import(client, admin, "ocarina-of-time", env.import_dir / "oot.z64")
    wait_job(
        client, client.post("/api/v1/recomps/shipwright/update", headers=admin).json()["id"], admin
    )
    if not rom_first:
        do_import(client, admin, "ocarina-of-time", env.import_dir / "oot.z64")
    wait_idle(client, admin)
    return rom


def plan(client, h, platform="windows-x64") -> dict:
    return client.get(
        "/api/v1/recomps/shipwright/install-plan", params={"platform": platform}, headers=h
    ).json()


# ---------------------------------------------------------------- automatic extraction


@pytest.mark.parametrize("rom_first", [True, False])
def test_soh_extracts_automatically(client, admin, github, env: Env, backend, rom_first):
    """Whichever arrives last (the release or the ROM) starts the extraction."""
    soh_setup(client, admin, github, env, rom_first=rom_first)
    overview = client.get("/api/v1/recomps/shipwright/builds", headers=admin).json()
    [b] = overview["builds"]
    assert (b["kind"], b["platform"], b["status"], b["auto"]) == (
        "extract",
        "any",
        "succeeded",
        True,
    )
    assert [o["name"] for o in b["outputs"]] == ["oot.o2r"]

    run = backend.runs[0]
    assert run["network"] is False
    assert run["env"]["RELEASE"] == "/release/SoH-Test.appimage"  # release_platform: linux-x64
    assert run["mounts"]["/release/SoH-Test.appimage"][1] is True  # read-only
    assert run["mounts"]["/input/oot/oot.z64"][1] is True
    assert run["mounts"]["/output"][1] is False
    assert backend.images[0].startswith("recompmanager-builder-soh-extract:")

    # Every platform now gets the .o2r instead of the ROM.
    for platform, first in (
        ("windows-x64", "SoH-Test-Win64.zip"),
        ("linux-x64", "SoH-Test.appimage"),
    ):
        p = plan(client, admin, platform)
        assert p["ready"] and p["strategy"] == "server_extract", p
        names = [s.get("name") for s in p["steps"] if s["type"] == "download_artifact"]
        assert names == [first, "oot.o2r"]
        assert not [s for s in p["steps"] if s["type"] == "download_input"]
    o2r = next(s for s in plan(client, admin)["steps"] if s.get("name") == "oot.o2r")
    got = client.get(o2r["url"], headers=admin)
    assert hashlib.sha256(got.content).hexdigest() == o2r["sha256"]
    # The work folder is cleaned up; the output stays.
    base = env.data_dir / "builds/ocarina-of-time/shipwright/any/9.9.9"
    assert (base / "output/oot.o2r").exists() and not (base / "work").exists()
    assert "fake build done" in (base / "build.log").read_text()


def test_fallback_plan_until_built(client, admin, github, env: Env, backend):
    backend.reason = "the Docker socket isn't mounted"
    soh_setup(client, admin, github, env, rom_first=True)
    assert client.get("/api/v1/recomps/shipwright/builds", headers=admin).json()["builds"] == []
    p = plan(client, admin)
    assert p["ready"] and p["strategy"] == "runtime"
    assert any("hasn't extracted the game data yet" in w for w in p["warnings"])
    assert [s["type"] for s in p["steps"]] == ["download_artifact", "download_input"]
    assert_error(
        client.post("/api/v1/recomps/shipwright/builds", headers=admin), 409, "builds_unavailable"
    )
    info = client.get("/api/v1/system/info", headers=admin).json()
    assert "isn't mounted" in info["builds_disabled_reason"]


def test_auto_build_setting(client, admin, github, env: Env, backend):
    client.patch("/api/v1/system/settings", json={"auto_build": False}, headers=admin)
    soh_setup(client, admin, github, env, rom_first=True)
    assert backend.runs == []
    r = client.post("/api/v1/recomps/shipwright/builds", headers=admin)
    assert r.status_code == 200 and r.json()[0]["auto"] is False
    wait_idle(client, admin)
    assert plan(client, admin)["strategy"] == "server_extract"


# ---------------------------------------------------------------- failures


def test_failed_build_is_not_retried_automatically(client, admin, github, env: Env, backend):
    backend.behaviour = "fail"
    soh_setup(client, admin, github, env, rom_first=True)
    [b] = client.get("/api/v1/recomps/shipwright/builds", headers=admin).json()["builds"]
    assert b["status"] == "failed" and "exited with code 2" in b["error"]
    assert "something broke" in b["error"]
    # Another trigger with the same inputs doesn't loop.
    do_import(client, admin, "ocarina-of-time", env.import_dir / "oot.z64", mode="reference")
    wait_idle(client, admin)
    assert len(client.get("/api/v1/recomps/shipwright/builds", headers=admin).json()["builds"]) == 1
    p = plan(client, admin)
    assert p["strategy"] == "runtime"  # fallback still works
    # A manual retry is allowed.
    backend.behaviour = "ok"
    client.post("/api/v1/recomps/shipwright/builds", headers=admin)
    wait_idle(client, admin)
    assert plan(client, admin)["strategy"] == "server_extract"


def test_cancel_build(client, admin, github, env: Env, backend):
    backend.behaviour = "hang"
    client.patch("/api/v1/system/settings", json={"auto_build": False}, headers=admin)
    soh_setup(client, admin, github, env, rom_first=True)
    [b] = client.post("/api/v1/recomps/shipwright/builds", headers=admin).json()
    assert backend.started.wait(10)
    p = plan(client, admin)
    assert any("working on it" in w for w in p["warnings"])
    client.post(f"/api/v1/jobs/{b['job_id']}/cancel", headers=admin)
    assert wait_job(client, b["job_id"], admin)["status"] == "cancelled"
    assert client.get(f"/api/v1/builds/{b['id']}", headers=admin).json()["status"] == "cancelled"


def test_resource_checks(client, admin, github, env: Env, backend, monkeypatch):
    from recompmanager import builder

    monkeypatch.setattr(builder, "host_memory_gb", lambda: 8.0)
    github.add_repo("kuraeow/ultratears", "UT")
    github.publish(
        "kuraeow/ultratears",
        "v9",
        {"UltraTears.AppImage": blob("ut"), "UltraTears-9-windows-x86_64.zip": blob("utw")},
    )
    add(client, admin, "https://github.com/kuraeow/ultratears")
    wait_idle(client, admin)
    wait_job(
        client, client.post("/api/v1/recomps/ultratears/update", headers=admin).json()["id"], admin
    )
    from tests.dumps import switch_folder

    do_import(
        client,
        admin,
        "tears-of-the-kingdom",
        switch_folder(env.import_dir / "totk"),
        mode="reference",
    )
    wait_idle(client, admin)
    [b] = client.get("/api/v1/recomps/ultratears/builds", headers=admin).json()["builds"]
    assert b["status"] == "failed" and "16 GB of RAM" in b["error"] and "8 GB" in b["error"]
    assert backend.runs == []  # refused before starting a container
    targets = client.get("/api/v1/recomps/ultratears/builds", headers=admin).json()["targets"]
    assert targets == [
        {
            "kind": "build",
            "platform": "linux-x64",
            "builder": "ultratears-linux",
            "network": True,
            "min_memory_gb": 16,
            "min_disk_gb": 100,
        }
    ]
    deck = client.get(
        "/api/v1/recomps/ultratears/install-plan",
        params={"platform": "linux-x64-steamdeck"},
        headers=admin,
    ).json()
    assert deck["strategy"] == "client_build"  # fallback while no build exists


def test_build_log_stream_and_prune(client, admin, github, env: Env, backend):
    soh_setup(client, admin, github, env, rom_first=True)
    [b] = client.get("/api/v1/recomps/shipwright/builds", headers=admin).json()["builds"]
    r = client.get(f"/api/v1/builds/{b['id']}/log", headers=admin)
    events = [blk for blk in r.text.split("\n\n") if blk.strip()]
    assert any('"fake build done"' in e for e in events)
    assert events[-1].startswith("event: end") and '"succeeded"' in events[-1]

    # Removing the version's files also removes what was built from it.
    client.post("/api/v1/recomps/shipwright/versions/9.9.9/pin", headers=admin)  # keep current safe
    github.publish(
        SOH, "10.0.0", {"SoH-Next-Win64.zip": blob("w2"), "SoH-Next.appimage": blob("a2")}
    )
    wait_job(
        client, client.post("/api/v1/recomps/shipwright/check", headers=admin).json()["id"], admin
    )
    wait_job(
        client, client.post("/api/v1/recomps/shipwright/update", headers=admin).json()["id"], admin
    )
    wait_idle(client, admin)
    client.delete("/api/v1/recomps/shipwright/versions/9.9.9/pin", headers=admin)
    assert (
        client.delete("/api/v1/recomps/shipwright/versions/9.9.9", headers=admin).status_code == 200
    )
    assert not (env.data_dir / "builds/ocarina-of-time/shipwright/any/9.9.9").exists()
    assert (env.data_dir / "builds/ocarina-of-time/shipwright/any/10.0.0/output/oot.o2r").exists()


# ---------------------------------------------------------------- host paths


def test_map_host_path():
    mounts = {
        "/data": "/mnt/user/recomps",
        "/roms": "/mnt/user/roms",
        "/roms/n64": "/mnt/disk2/n64",
    }
    m = lambda p, **kw: map_host_path(p, mounts, "/data", None, in_container=True, **kw)  # noqa: E731
    assert m("/data/library/x") == "/mnt/user/recomps/library/x"
    assert m("/roms/arcade/vf2.zip") == "/mnt/user/roms/arcade/vf2.zip"
    assert m("/roms/n64/oot.z64") == "/mnt/disk2/n64/oot.z64"  # longest match wins
    for outside in ("/romsx/a", "/somewhere/else"):  # /romsx isn't inside /roms
        with pytest.raises(BuildError):
            m(outside)
    assert (
        map_host_path("/data/a", {}, "/data", "/mnt/cache/r", in_container=True) == "/mnt/cache/r/a"
    )
    assert map_host_path("/tmp/x", {}, "/data", None, in_container=False) == "/tmp/x"


# ---------------------------------------------------------------- real Docker (CI)


@pytest.mark.skipif(
    os.environ.get("RM_TEST_DOCKER") != "1", reason="set RM_TEST_DOCKER=1 to use Docker"
)
def test_real_docker_build(env: Env, github: FakeGitHub, admin_token, tmp_path):
    """A tiny busybox builder through the real Docker daemon: image build, bind mounts
    (read-only inputs), running as our user, log streaming, outputs."""
    env.docker_socket = Path("/var/run/docker.sock")
    builders = env.recipes_dir / "builders/test-busybox"
    builders.mkdir(parents=True)
    (builders / "Dockerfile").write_text("FROM busybox:1.36\nCOPY run.sh /run.sh\n")
    (builders / "run.sh").write_text(
        'set -eu\necho building $VERSION\ncat "$RELEASE" "$INPUT_INPUT" > "$OUTPUT/game.tar"\n'
        'touch "$INPUT_INPUT" 2>/dev/null && echo INPUT-WRITABLE || echo input is read-only\n'
    )
    h = {"Authorization": f"Bearer {admin_token}"}
    github.add_repo("someone/real", "A recomp of Real Game")
    github.publish("someone/real", "v1", {"Real-linux-x64.tar.gz": b"RELEASE-BYTES"})
    with TestClient(create_app(env, http_transport=github.transport())) as c:
        r = add(c, h, "someone/real")
        recipe = r["recipe"]
        recipe["input"] = {"kind": "file", "description": "disc"}
        recipe["platforms"] = {
            "linux-x64": {
                "strategy": "server_build",
                "build": {
                    "builder": "test-busybox",
                    "command": ["sh", "/run.sh"],
                    "outputs": ["*.tar"],
                },
                "steps": [{"place_output": {"to": "{install}", "extract": False}}],
            }
        }
        c.patch("/api/v1/recomps/real", json={"recipe": recipe, "status": "active"}, headers=h)
        wait_idle(c, h)
        wait_job(c, c.post("/api/v1/recomps/real/update", headers=h).json()["id"], h)
        env.import_dir.mkdir(parents=True, exist_ok=True)
        (env.import_dir / "disc.bin").write_bytes(b"-INPUT-BYTES")
        do_import(c, h, "real-game", env.import_dir / "disc.bin", mode="reference")
        wait_idle(c, h, timeout=300)
        [b] = c.get("/api/v1/recomps/real/builds", headers=h).json()["builds"]
        assert b["status"] == "succeeded", b
        out = c.get(f"/api/v1/artifacts/{b['outputs'][0]['artifact_id']}/download", headers=h)
        assert out.content == b"RELEASE-BYTES-INPUT-BYTES"
        log = c.get(f"/api/v1/builds/{b['id']}/log", headers=h).text
        assert "building v1" in log and "input is read-only" in log
        out_file = next((env.data_dir / "builds").rglob("game.tar"))
        assert out_file.stat().st_uid == os.getuid()  # ran as our user, not root
    shutil.rmtree(builders)


def test_docker_backend_unavailable_without_socket(env: Env):
    assert "isn't mounted" in DockerBackend(env).unavailable()
