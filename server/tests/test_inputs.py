"""Inputs vault: import, upload, verification, download. Synthetic dumps only."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from recompmanager import formats
from recompmanager.config import Env
from tests.conftest import wait_idle, wait_job
from tests.dumps import (
    fake_n64_rom,
    make_npdm,
    make_param_sfo,
    make_ps4_pkg,
    ps4_folder,
    switch_folder,
    zip_with,
)
from tests.fakegithub import FakeGitHub
from tests.test_auth import assert_error
from tests.test_library import BUILTIN_REPOS, add

SOH_HASHES = (
    "https://raw.githubusercontent.com/HarbourMasters/Shipwright/develop/docs/supportedHashes.json"
)


# ---------------------------------------------------------------- formats


def test_n64_byte_orders_hash_alike():
    z = formats.hash_stream(io.BytesIO(fake_n64_rom("a", order="z64")).read)
    v = formats.hash_stream(io.BytesIO(fake_n64_rom("a", order="v64")).read)
    n = formats.hash_stream(io.BytesIO(fake_n64_rom("a", order="n64")).read)
    assert z["n64_order"] == "z64" and v["n64_order"] == "v64" and n["n64_order"] == "n64"
    assert z["sha1_z64"] == v["sha1_z64"] == n["sha1_z64"] == z["sha1"]
    assert v["sha1"] != z["sha1"]


def test_param_sfo_npdm_pkg():
    sfo = formats.parse_param_sfo(
        make_param_sfo({"TITLE_ID": "CUSA03173", "APP_VER": "01.09", "X": 7})
    )
    assert sfo == {"TITLE_ID": "CUSA03173", "APP_VER": "01.09", "X": 7}
    assert formats.parse_npdm_program_id(make_npdm("0100F2C0115B6000")) == "0100F2C0115B6000"
    assert formats.parse_npdm_program_id(b"junk") is None
    assert formats.content_id_title("UP9000-CUSA03173_00-BLOODBORNE000000") == "CUSA03173"
    assert (
        formats.switch_title_id_from_name("TotK [0100F2C0115B6000][v0].nsp") == "0100F2C0115B6000"
    )


def test_pkg_content_id(tmp_path):
    p = tmp_path / "game.pkg"
    p.write_bytes(make_ps4_pkg("UP9000-CUSA03173_00-BLOODBORNE000000"))
    assert formats.ps4_pkg_content_id(p) == "UP9000-CUSA03173_00-BLOODBORNE000000"


# ---------------------------------------------------------------- helpers


@pytest.fixture
def lib(client: TestClient, admin, github: FakeGitHub) -> TestClient:
    github.load_all_fixtures()
    for url in BUILTIN_REPOS.values():
        add(client, admin, url)
    wait_idle(client, admin)
    return client


def do_import(client, admin, game: str, path, **kw) -> dict:
    r = client.post(
        f"/api/v1/games/{game}/inputs/import", json={"path": str(path), **kw}, headers=admin
    )
    assert r.status_code == 202, r.text
    return wait_job(client, r.json()["id"], admin)


def inputs(client, admin, game: str) -> list[dict]:
    return client.get(f"/api/v1/games/{game}/inputs", headers=admin).json()


def fit(inp: dict, recomp: str) -> dict:
    return next(f for f in inp["fits"] if f["recomp_id"] == recomp)


# ---------------------------------------------------------------- importing and verifying


def test_import_file_copy_unverified(lib, admin, env: Env):
    src = zip_with(env.import_dir / "arcade/vf2.zip", {"fake.bin": b"not a real rom"})
    job = do_import(lib, admin, "virtua-fighter-2", src)
    assert job["status"] == "succeeded", job
    [inp] = inputs(lib, admin, "virtua-fighter-2")
    assert inp["state"] == "ready" and inp["storage"] == "vault" and inp["name"] == "vf2.zip"
    assert inp["sha1"] == hashlib.sha1(src.read_bytes()).hexdigest()
    assert inp["facts"]["zip_entries"][0]["name"] == "fake.bin"
    assert fit(inp, "vf2-recomp")["status"] == "unverified"
    assert src.exists()  # copy leaves the original alone
    stored = list((env.data_dir / "inputs/virtua-fighter-2").rglob("vf2.zip"))
    assert len(stored) == 1


def test_soh_rom_matched_by_hash_list_in_any_byte_order(lib, admin, env: Env, github):
    normal = fake_n64_rom("oot-pal10")
    mq = fake_n64_rom("oot-mq")
    github.blobs[SOH_HASHES] = json.dumps(
        [
            {"name": "PAL 1.0", "sha1": hashlib.sha1(normal).hexdigest()},
            {"name": "PAL MQ", "sha1": hashlib.sha1(mq).hexdigest()},
        ]
    ).encode()
    (env.import_dir).mkdir(parents=True, exist_ok=True)
    (env.import_dir / "oot.v64").write_bytes(fake_n64_rom("oot-pal10", order="v64"))
    (env.import_dir / "mq.z64").write_bytes(mq)
    (env.import_dir / "other.z64").write_bytes(fake_n64_rom("something-else"))
    for name in ("oot.v64", "mq.z64", "other.z64"):
        assert (
            do_import(lib, admin, "ocarina-of-time", env.import_dir / name)["status"] == "succeeded"
        )
    by_name = {i["name"]: i for i in inputs(lib, admin, "ocarina-of-time")}
    assert fit(by_name["oot.v64"], "shipwright") == {
        "recomp_id": "shipwright",
        "slot": "oot",
        "status": "verified",
        "reason": "Matches PAL 1.0",
    }
    assert by_name["oot.v64"]["facts"]["n64_order"] == "v64"
    mq_fit = fit(by_name["mq.z64"], "shipwright")
    assert (mq_fit["slot"], mq_fit["status"]) == ("oot-mq", "verified")
    assert fit(by_name["other.z64"], "shipwright")["status"] == "mismatch"


def test_ps4_folder_verified_by_title_id_and_version(lib, admin, env: Env):
    good = ps4_folder(env.import_dir / "CUSA03173")
    old = ps4_folder(env.import_dir / "old/CUSA03173", app_ver="01.00")
    wrong = ps4_folder(env.import_dir / "CUSA00900", title_id="CUSA00900")
    for p in (good, old, wrong):
        assert do_import(lib, admin, "bloodborne", p, mode="reference")["status"] == "succeeded"
    ins = inputs(lib, admin, "bloodborne")
    by_ver = {(i["facts"]["ps4"]["TITLE_ID"], i["facts"]["ps4"]["APP_VER"]): i for i in ins}
    assert fit(by_ver[("CUSA03173", "01.09")], "lantern-engine")["status"] == "verified"
    f = fit(by_ver[("CUSA03173", "01.00")], "lantern-engine")
    assert f["status"] == "mismatch" and "needs 01.09" in f["reason"]
    assert fit(by_ver[("CUSA00900", "01.09")], "lantern-engine")["status"] == "mismatch"
    assert all(i["storage"] == "reference" for i in ins)
    assert not (env.data_dir / "inputs/bloodborne").exists()  # nothing copied


def test_switch_folder_and_incremental_rehash(lib, admin, env: Env):
    src = switch_folder(env.import_dir / "totk", files=6)
    assert (
        do_import(lib, admin, "tears-of-the-kingdom", src, mode="reference")["status"]
        == "succeeded"
    )
    [inp] = inputs(lib, admin, "tears-of-the-kingdom")
    assert inp["kind"] == "folder" and inp["file_count"] == 8
    assert inp["facts"]["switch"]["program_id"] == "0100F2C0115B6000"
    f = fit(inp, "ultratears")
    assert f["status"] == "verified" and "version not checked" in f["reason"]

    (src / "romfs/dir0/file0.bin").write_bytes(b"changed!")
    job = lib.post(f"/api/v1/inputs/{inp['id']}/rehash", headers=admin).json()
    done = wait_job(lib, job["id"], admin)
    assert "1 to hash" in done["log"]

    # Removing a required path turns it into a mismatch.
    (src / "exefs/main.npdm").unlink()
    wait_job(lib, lib.post(f"/api/v1/inputs/{inp['id']}/rehash", headers=admin).json()["id"], admin)
    f = fit(inputs(lib, admin, "tears-of-the-kingdom")[0], "ultratears")
    assert f["status"] == "mismatch" and "exefs/main.npdm" in f["reason"]


def test_missing_reference(lib, admin, env: Env):
    src = ps4_folder(env.import_dir / "CUSA03173")
    do_import(lib, admin, "bloodborne", src, mode="reference")
    [inp] = inputs(lib, admin, "bloodborne")
    import shutil

    shutil.rmtree(src)
    job = wait_job(
        lib, lib.post(f"/api/v1/inputs/{inp['id']}/rehash", headers=admin).json()["id"], admin
    )
    assert job["status"] == "failed"
    assert inputs(lib, admin, "bloodborne")[0]["state"] == "missing"
    assert_error(lib.get(f"/api/v1/inputs/{inp['id']}/download", headers=admin), 409, "not_ready")


def test_import_from_zip_member_and_whole_archive(lib, admin, env: Env):
    rom = fake_n64_rom("zipped")
    archive = zip_with(
        env.roms_dir / "N64/OoT (Europe).zip", {"OoT (Europe).z64": rom, "readme.txt": b"hi"}
    )
    job = do_import(lib, admin, "ocarina-of-time", archive, member="OoT (Europe).z64")
    assert job["status"] == "succeeded", job
    [inp] = inputs(lib, admin, "ocarina-of-time")
    assert inp["name"] == "OoT (Europe).z64" and inp["sha1"] == hashlib.sha1(rom).hexdigest()

    folder_zip = zip_with(
        env.import_dir / "bb.zip",
        {
            "CUSA03173/eboot.bin": b"e",
            "CUSA03173/sce_sys/param.sfo": make_param_sfo(
                {"TITLE_ID": "CUSA03173", "APP_VER": "01.09"}
            ),
        },
    )
    job = do_import(lib, admin, "bloodborne", folder_zip, extract=True)
    assert job["status"] == "succeeded", job
    [bb] = inputs(lib, admin, "bloodborne")
    assert bb["kind"] == "folder" and bb["facts"]["ps4"]["TITLE_ID"] == "CUSA03173"
    assert fit(bb, "lantern-engine")["status"] == "verified"


def test_import_rejects_paths_outside_roots(lib, admin, env: Env, tmp_path):
    (tmp_path / "secret").write_text("x")
    r = lib.post(
        "/api/v1/games/bloodborne/inputs/import",
        json={"path": str(tmp_path / "secret")},
        headers=admin,
    )
    assert_error(r, 422, "invalid_path")
    env.import_dir.mkdir(parents=True, exist_ok=True)
    r = lib.post(
        "/api/v1/games/bloodborne/inputs/import",
        json={"path": str(env.import_dir / ".." / "secret")},
        headers=admin,
    )
    assert_error(r, 422, "invalid_path")


def test_browse(lib, admin, env: Env):
    zip_with(env.import_dir / "a/vf2.zip", {"x": b"y"})
    env.roms_dir.mkdir(parents=True, exist_ok=True)
    top = lib.get("/api/v1/system/browse", headers=admin).json()
    assert {e["name"] for e in top["entries"]} == {"import", "roms"}
    sub = lib.get(
        "/api/v1/system/browse", params={"path": str(env.import_dir / "a")}, headers=admin
    ).json()
    assert [e["name"] for e in sub["entries"]] == ["vf2.zip"]
    assert sub["parent"] == str(env.import_dir.resolve())


# ---------------------------------------------------------------- upload


def test_resumable_upload(lib, admin):
    data = fake_n64_rom("uploaded", size=300_000)
    up = lib.post(
        "/api/v1/games/ocarina-of-time/uploads",
        json={"filename": "C:\\roms\\my oot.z64", "size": len(data)},
        headers=admin,
    ).json()
    assert up["filename"] == "my oot.z64"
    h = {**admin, "Content-Type": "application/octet-stream"}
    r = lib.put(f"/api/v1/uploads/{up['id']}?offset=0", content=data[:100_000], headers=h)
    assert r.json()["received"] == 100_000
    assert_error(
        lib.put(f"/api/v1/uploads/{up['id']}?offset=0", content=b"x", headers=h),
        409,
        "wrong_offset",
    )
    assert_error(lib.post(f"/api/v1/uploads/{up['id']}/complete", headers=admin), 409, "incomplete")
    # A client resumes from what the server says it has.
    have = lib.get(f"/api/v1/uploads/{up['id']}", headers=admin).json()["received"]
    lib.put(f"/api/v1/uploads/{up['id']}?offset={have}", content=data[have:], headers=h)
    job = lib.post(f"/api/v1/uploads/{up['id']}/complete", headers=admin).json()
    assert wait_job(lib, job["id"], admin)["status"] == "succeeded"
    [inp] = inputs(lib, admin, "ocarina-of-time")
    assert inp["source"] == "upload" and inp["sha256"] == hashlib.sha256(data).hexdigest()
    assert_error(
        lib.put(f"/api/v1/uploads/{up['id']}?offset=0", content=b"x", headers=h), 404, "not_found"
    )


def test_upload_too_large(lib, admin):
    up = lib.post(
        "/api/v1/games/ocarina-of-time/uploads",
        json={"filename": "a.z64", "size": 10},
        headers=admin,
    ).json()
    r = lib.put(
        f"/api/v1/uploads/{up['id']}?offset=0",
        content=b"x" * 11,
        headers={**admin, "Content-Type": "application/octet-stream"},
    )
    assert_error(r, 413, "too_large")


# ---------------------------------------------------------------- downloading


def test_download_file_folder_and_manifest(lib, admin, reader, env: Env):
    src = switch_folder(env.import_dir / "totk", files=4)
    do_import(lib, admin, "tears-of-the-kingdom", src)  # copy into the vault
    [inp] = inputs(lib, admin, "tears-of-the-kingdom")

    # Clients (read tokens) can fetch inputs; that's the point of the vault.
    r = lib.get(f"/api/v1/inputs/{inp['id']}/download", headers=reader)
    assert r.headers["content-type"] == "application/x-tar"
    with tarfile.open(fileobj=io.BytesIO(r.content)) as t:
        names = sorted(m.name for m in t.getmembers())
        assert t.extractfile("exefs/main").read() == b"fake main"
    assert names == sorted(
        ["exefs/main", "exefs/main.npdm"] + [f"romfs/dir{i % 2}/file{i}.bin" for i in range(4)]
    )

    m = lib.get(f"/api/v1/inputs/{inp['id']}/manifest", headers=reader).json()
    assert m["total_size"] == inp["size"] and len(m["files"]) == 6
    f = next(x for x in m["files"] if x["path"] == "romfs/dir1/file3.bin")
    got = lib.get(f"/api/v1/inputs/{inp['id']}/files/romfs/dir1/file3.bin", headers=reader)
    assert (
        got.content == bytes([3]) * 103 and hashlib.sha256(got.content).hexdigest() == f["sha256"]
    )
    part = lib.get(
        f"/api/v1/inputs/{inp['id']}/files/romfs/dir1/file3.bin",
        headers={**reader, "Range": "bytes=0-9"},
    )
    assert part.status_code == 206 and len(part.content) == 10
    assert_error(
        lib.get(f"/api/v1/inputs/{inp['id']}/files/../secret", headers=reader), 404, "not_found"
    )

    rom = fake_n64_rom("dl")
    (env.import_dir / "dl.z64").write_bytes(rom)
    do_import(lib, admin, "ocarina-of-time", env.import_dir / "dl.z64")
    [oot] = inputs(lib, admin, "ocarina-of-time")
    r = lib.get(f"/api/v1/inputs/{oot['id']}/download", headers={**reader, "Range": "bytes=4-7"})
    assert r.status_code == 206 and r.content == rom[4:8]


def test_inputs_need_a_token(lib):
    assert_error(lib.get("/api/v1/inputs/1/download"), 401, "unauthorized")
    assert_error(lib.get("/api/v1/games/bloodborne/inputs"), 401, "unauthorized")


def test_delete_input(lib, admin, env: Env):
    src = ps4_folder(env.import_dir / "CUSA03173")
    do_import(lib, admin, "bloodborne", src)  # vault copy
    do_import(lib, admin, "bloodborne", src, mode="reference")
    vault, ref = sorted(inputs(lib, admin, "bloodborne"), key=lambda i: i["storage"], reverse=True)
    assert vault["storage"] == "vault" and ref["storage"] == "reference"
    assert lib.delete(f"/api/v1/inputs/{vault['id']}", headers=admin).status_code == 204
    assert lib.delete(f"/api/v1/inputs/{ref['id']}", headers=admin).status_code == 204
    assert not (env.data_dir / "inputs/bloodborne").exists()
    assert (src / "eboot.bin").exists()  # referenced dumps are never touched
