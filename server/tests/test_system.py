from __future__ import annotations

from fastapi.testclient import TestClient

from recompmanager.config import Env
from recompmanager.main import create_app
from tests.test_auth import assert_error


def test_settings_defaults_and_patch(client: TestClient, admin):
    s = client.get("/api/v1/system/settings", headers=admin).json()
    assert s == {
        "update_check_interval_hours": 6,
        "keep_versions": 2,
        "auto_update_default": True,
        "github_token_set": False,
        "rom_scan_interval_hours": 24,
        "auto_accept_scan_matches": True,
        "auto_build": True,
    }
    r = client.patch(
        "/api/v1/system/settings",
        json={"keep_versions": 3, "github_token": "ghp_secret"},
        headers=admin,
    )
    assert r.status_code == 200
    assert r.json()["keep_versions"] == 3
    assert r.json()["github_token_set"] is True
    assert "ghp_secret" not in r.text

    r = client.patch("/api/v1/system/settings", json={"github_token": ""}, headers=admin)
    assert r.json()["github_token_set"] is False
    assert r.json()["keep_versions"] == 3


def test_settings_validation_uses_envelope(client: TestClient, admin):
    r = client.patch("/api/v1/system/settings", json={"keep_versions": 0}, headers=admin)
    assert_error(r, 422, "validation_error")
    assert r.json()["error"]["details"][0]["loc"][-1] == "keep_versions"


def test_unknown_api_path_uses_envelope(client: TestClient, admin):
    assert_error(client.get("/api/v1/nope", headers=admin), 404, "not_found")


def test_system_info_without_docker(client: TestClient, admin):
    info = client.get("/api/v1/system/info", headers=admin).json()
    assert info["docker_available"] is False
    assert "Docker socket" in info["builds_disabled_reason"]


def test_system_info_socket_that_does_not_answer(env: Env, admin):
    env.docker_socket.touch()  # a file, not a daemon
    with TestClient(create_app(env)) as c:
        info = c.get("/api/v1/system/info", headers=admin).json()
    assert info["docker_available"] is True
    assert "isn't answering" in info["builds_disabled_reason"]


def test_creates_data_layout(client: TestClient, env: Env):
    for sub in ("library", "inputs", "builds"):
        assert (env.data_dir / sub).is_dir()
    assert env.recipes_dir.is_dir()


def test_frontend_served_with_spa_fallback(env: Env, tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>app</html>")
    (web / "assets" / "a.js").write_text("x")
    (tmp_path / "secret.txt").write_text("no")
    env.static_dir = web
    with TestClient(create_app(env)) as c:
        assert c.get("/").text == "<html>app</html>"
        assert c.head("/").status_code == 200
        assert c.get("/library/vf2").text == "<html>app</html>"
        assert c.get("/assets/a.js").text == "x"
        assert c.get("/../secret.txt").text != "no"
        assert c.get("/api/v1/system/health").json()["status"] == "ok"


def test_openapi_operation_ids_unique(client: TestClient):
    spec = client.get("/api/v1/openapi.json").json()
    ids = [op["operationId"] for ops in spec["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids))
    assert "get_health" in ids and "create_api_token" in ids
