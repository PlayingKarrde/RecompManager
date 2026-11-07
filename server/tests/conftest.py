from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recompmanager.config import Env
from recompmanager.main import create_app


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        import_dir=tmp_path / "import",
        docker_socket=tmp_path / "docker.sock",
        static_dir=None,
    )


@pytest.fixture
def client(env: Env) -> Iterator[TestClient]:
    with TestClient(create_app(env)) as c:
        yield c


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
