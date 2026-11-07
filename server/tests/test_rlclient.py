"""The reference client installs from real install plans (against the test server)."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile

import pytest
from fastapi.testclient import TestClient
from rlclient import cli
from rlclient.api import Api
from rlclient.installer import Installer, InstallError, Places, extract

from recompmanager.config import Env
from tests.conftest import wait_idle, wait_job
from tests.dumps import ps4_folder, zip_with
from tests.fakegithub import FakeGitHub
from tests.test_inputs import do_import
from tests.test_library import add


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


def tgz_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for n, d in files.items():
            info = tarfile.TarInfo(n)
            info.size = len(d)
            info.mode = 0o755 if n.endswith(".sh") else 0o644
            t.addfile(info, io.BytesIO(d))
    return buf.getvalue()


VF2_WIN = zip_bytes(
    {"VirtuaFighter2/VirtuaFighter2.exe": b"MZ fake", "VirtuaFighter2/SDL3.dll": b"d"}
)
VF2_DECK = tgz_bytes(
    {"VirtuaFighter2/VirtuaFighter2.sh": b"#!/bin/sh\n", "VirtuaFighter2/vf2": b"elf"}
)


@pytest.fixture
def server(client: TestClient, admin, github: FakeGitHub, env: Env) -> TestClient:
    github.add_repo("mr-finster/vf2-recomp", "VF2")
    github.publish(
        "mr-finster/vf2-recomp",
        "v1.0.0",
        {
            "VirtuaFighter2Recomp-windows.zip": VF2_WIN,
            "VirtuaFighter2Recomp-deck.tar.gz": VF2_DECK,
            "VirtuaFighter2Recomp-mac.zip": zip_bytes({"Virtua Fighter 2 Recomp/README.txt": b"r"}),
            "VirtuaFighter2Recomp-linux-arm64.tar.gz": VF2_DECK,
        },
    )
    add(client, admin, "mr-finster/vf2-recomp")
    wait_idle(client, admin)
    assert (
        wait_job(
            client,
            client.post("/api/v1/recomps/vf2-recomp/update", headers=admin).json()["id"],
            admin,
        )["status"]
        == "succeeded"
    )
    zip_with(env.import_dir / "vf2.zip", {"epr-1.bin": b"fake rom set"})
    do_import(client, admin, "virtua-fighter-2", env.import_dir / "vf2.zip")
    return client


@pytest.fixture
def api(server: TestClient, reader) -> Api:
    token = reader["Authorization"].split()[1]
    return Api("http://testserver", token, client=server)


def run_cli(argv, api, tmp_path) -> tuple[int, cli.Config]:
    cfg = cli.Config(tmp_path / "rlclient.json")
    return cli.main(argv, api=api, config=cfg), cfg


def test_install_vf2_windows(api, tmp_path, env: Env, monkeypatch):
    monkeypatch.setattr("rlclient.platforms.data_dir", lambda: tmp_path / "data")
    install = tmp_path / "Games/VF2"
    cfg = cli.Config(tmp_path / "rlclient.json")
    assert (
        cli.main(
            [
                "login",
                "http://testserver",
                "--token",
                "x",
                "--name",
                "PC",
                "--platform",
                "windows-x64",
            ],
            api=api,
            config=cfg,
        )
        == 0
    )
    assert cli.main(["install", "vf2-recomp", "--dir", str(install)], api=api, config=cfg) == 0

    assert (install / "VirtuaFighter2/VirtuaFighter2.exe").read_bytes() == b"MZ fake"
    assert (install / "VirtuaFighter2/portable.txt").read_text() == ""
    rom = install / "VirtuaFighter2/roms/vf2.zip"
    assert rom.read_bytes() == (env.import_dir / "vf2.zip").read_bytes()
    saved = json.loads((tmp_path / "rlclient.json").read_text())
    assert saved["installs"]["vf2-recomp"]["version"] == "v1.0.0"
    assert saved["installs"]["vf2-recomp"]["launch"]["exe"] == "VirtuaFighter2/VirtuaFighter2.exe"

    # The server knows what this device has installed.
    clients = api.http.get(
        "/api/v1/clients", headers={"Authorization": "Bearer " + _admin_token(env)}
    ).json()
    assert clients[0]["name"] == "PC" and clients[0]["installed"] == {"vf2-recomp": "v1.0.0"}

    # Running it again downloads nothing: every file is already there and verified.
    streamed = []
    real_stream = api.http.stream

    def counting_stream(method, url, **kw):
        streamed.append(url)
        return real_stream(method, url, **kw)

    monkeypatch.setattr(api.http, "stream", counting_stream)
    assert cli.main(["install", "vf2-recomp"], api=api, config=cfg) == 0
    assert streamed == []


def _admin_token(env: Env) -> str:
    from tests.conftest import read_bootstrap_token

    return read_bootstrap_token(env)


def test_install_vf2_steam_deck(api, tmp_path, monkeypatch):
    monkeypatch.setattr("rlclient.platforms.data_dir", lambda: tmp_path / "data")
    install = tmp_path / "deck"
    code, _cfg = run_cli(
        ["install", "vf2-recomp", "--dir", str(install), "--platform", "linux-x64-steamdeck"],
        api,
        tmp_path,
    )
    assert code == 0
    assert (install / "VirtuaFighter2/VirtuaFighter2.sh").exists()
    assert (install / "VirtuaFighter2/roms/vf2.zip").exists()
    assert not (install / "VirtuaFighter2/portable.txt").exists()  # Linux build needs none


def test_install_refused_when_not_ready(server, admin, api, tmp_path, monkeypatch):
    monkeypatch.setattr("rlclient.platforms.data_dir", lambda: tmp_path / "data")
    code, _ = run_cli(
        ["install", "vf2-recomp", "--dir", str(tmp_path / "x"), "--platform", "windows-arm64"],
        api,
        tmp_path,
    )
    assert code == 3


def test_folder_input_via_manifest(client, admin, reader, github, env: Env, tmp_path):
    github.add_repo("Awakenchan/LanternEngine", "L")
    github.publish(
        "Awakenchan/LanternEngine",
        "v1-beta",
        {"LanternEngine-v1-beta-win64.zip": zip_bytes({"BloodbornePC/Bloodborne.exe": b"x"})},
        prerelease=True,
    )
    add(client, admin, "Awakenchan/LanternEngine")
    wait_idle(client, admin)
    wait_job(
        client,
        client.post("/api/v1/recomps/lantern-engine/update", headers=admin).json()["id"],
        admin,
    )
    src = ps4_folder(env.import_dir / "CUSA03173")
    do_import(client, admin, "bloodborne", src, mode="reference")
    api = Api("http://testserver", reader["Authorization"].split()[1], client=client)
    plan = api.get("/api/v1/recomps/lantern-engine/install-plan", platform="windows-x64")
    places = Places(install=tmp_path / "le", data=tmp_path / "data")
    Installer(api, places, yes=True).run(plan)
    assert (tmp_path / "le/Bloodborne.exe").exists()  # strip_root dropped BloodbornePC/
    got = tmp_path / "data/lantern-engine/CUSA03173"
    assert (got / "sce_sys/param.sfo").read_bytes() == (src / "sce_sys/param.sfo").read_bytes()
    assert (got / "dvdroot_ps4/data.bin").read_bytes() == b"x" * 1000


def test_run_step_needs_confirmation(tmp_path):
    places = Places(install=tmp_path, data=tmp_path / "d")
    inst = Installer(Api("http://x", "t"), places, ask=lambda _q: "n")
    with pytest.raises(InstallError):
        inst.step_run({"command": "echo hi", "cwd": "{install}"})
    inst = Installer(Api("http://x", "t"), places, ask=lambda _q: "y")
    inst.step_run({"command": "echo hi > out.txt", "cwd": "{install}"})
    assert (tmp_path / "out.txt").read_text().strip() == "hi"


def test_extract_refuses_escaping_paths(tmp_path):
    evil = tmp_path / "evil.zip"
    evil.write_bytes(zip_bytes({"../outside.txt": b"x"}))
    with pytest.raises(InstallError):
        extract(evil, tmp_path / "out", strip_root=False)
    assert not (tmp_path / "outside.txt").exists()


def test_extract_strip_root_tar(tmp_path):
    a = tmp_path / "a.tar.gz"
    a.write_bytes(tgz_bytes({"Top-1.2/run.sh": b"#!/bin/sh\n", "Top-1.2/sub/x": b"1"}))
    extract(a, tmp_path / "o", strip_root=True)
    assert (tmp_path / "o/run.sh").exists() and (tmp_path / "o/sub/x").read_bytes() == b"1"


def test_platform_detect():
    from rlclient.platforms import detect

    assert detect().split("-")[0] in ("windows", "linux", "macos", "android")
