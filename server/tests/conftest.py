from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recompmanager.config import Env
from recompmanager.main import create_app
from tests.fakegithub import FakeGitHub


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        import_dir=tmp_path / "import",
        roms_dir=tmp_path / "roms",
        docker_socket=tmp_path / "docker.sock",
        static_dir=None,
        scheduler=False,
    )


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def client(env: Env, github: FakeGitHub) -> Iterator[TestClient]:
    with TestClient(create_app(env, http_transport=github.transport())) as c:
        yield c


def wait_job(client: TestClient, job_id: int, headers: dict[str, str], timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} didn't finish: {job}")


def wait_idle(client: TestClient, headers: dict[str, str], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.get("/api/v1/jobs?active=true", headers=headers).json():
            return
        time.sleep(0.02)
    raise AssertionError("jobs still running")


def read_bootstrap_token(env: Env) -> str:
    lines = env.bootstrap_token_file.read_text(encoding="utf-8").splitlines()
    return next(line for line in lines if line.startswith("rm_"))


@pytest.fixture
def admin_token(client: TestClient, env: Env) -> str:
    return read_bootstrap_token(env)


@pytest.fixture
def admin(admin_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def reader(client: TestClient, admin: dict[str, str]) -> dict[str, str]:
    r = client.post("/api/v1/tokens", json={"name": "client", "scope": "read"}, headers=admin)
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['token']}"}
