from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from recompmanager.config import Env
from recompmanager.main import create_app
from recompmanager.models import ApiToken, utcnow
from tests.conftest import read_bootstrap_token


def assert_error(resp, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)


def test_health_needs_no_token(client: TestClient):
    r = client.get("/api/v1/system/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/logout"),
        ("PUT", "/api/v1/auth/password"),
        ("GET", "/api/v1/auth/sessions"),
        ("DELETE", "/api/v1/auth/sessions/1"),
        ("GET", "/api/v1/system/info"),
        ("GET", "/api/v1/system/settings"),
        ("PATCH", "/api/v1/system/settings"),
        ("GET", "/api/v1/tokens"),
        ("POST", "/api/v1/tokens"),
        ("DELETE", "/api/v1/tokens/1"),
    ],
)
def test_every_other_endpoint_needs_a_token(client: TestClient, method: str, path: str):
    assert_error(client.request(method, path), 401, "unauthorized")


def test_openapi_only_health_and_login_are_open(client: TestClient):
    spec = client.get("/api/v1/openapi.json").json()
    open_ops = {
        (path, method)
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if not op.get("security")
    }
    assert open_ops == {("/api/v1/system/health", "get"), ("/api/v1/auth/login", "post")}


def test_bad_token_rejected(client: TestClient):
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer rm_nope"})
    assert_error(r, 401, "unauthorized")


def test_bootstrap_token_is_admin(client: TestClient, admin):
    r = client.get("/api/v1/auth/me", headers=admin)
    assert r.status_code == 200
    assert r.json()["scope"] == "admin"
    assert r.json()["token_name"] == "bootstrap"


def test_bootstrap_only_once(env: Env, admin_token: str):
    # A second start with an admin token present must not mint another.
    env.bootstrap_token_file.unlink()
    with TestClient(create_app(env)):
        pass
    assert not env.bootstrap_token_file.exists()


def test_read_token_cannot_do_admin_things(client: TestClient, reader):
    assert client.get("/api/v1/auth/me", headers=reader).json()["scope"] == "read"
    assert client.get("/api/v1/system/info", headers=reader).status_code == 200
    assert_error(client.get("/api/v1/tokens", headers=reader), 403, "forbidden")
    assert_error(client.get("/api/v1/system/settings", headers=reader), 403, "forbidden")


def test_create_list_revoke_token(client: TestClient, admin):
    r = client.post("/api/v1/tokens", json={"name": "deck", "scope": "read"}, headers=admin)
    created = r.json()
    assert created["token"].startswith("rm_")
    assert created["token"].startswith(created["prefix"])

    listed = client.get("/api/v1/tokens", headers=admin).json()
    assert {t["name"] for t in listed} == {"bootstrap", "deck"}
    assert all("token" not in t and "token_hash" not in t for t in listed)

    new = {"Authorization": f"Bearer {created['token']}"}
    assert client.get("/api/v1/auth/me", headers=new).status_code == 200
    assert client.delete(f"/api/v1/tokens/{created['id']}", headers=admin).status_code == 204
    assert_error(client.get("/api/v1/auth/me", headers=new), 401, "unauthorized")


def test_cannot_revoke_own_token(client: TestClient, admin):
    me = client.get("/api/v1/auth/me", headers=admin).json()
    assert_error(client.delete(f"/api/v1/tokens/{me['token_id']}", headers=admin), 409, "conflict")


def test_no_active_admin_token_rebootstraps(env: Env, client: TestClient, admin, admin_token):
    # Recovery path: with every admin token revoked, the next start mints a new one.
    second = client.post("/api/v1/tokens", json={"name": "a2", "scope": "admin"}, headers=admin)
    h2 = {"Authorization": f"Bearer {second.json()['token']}"}
    boot_id = client.get("/api/v1/auth/me", headers=admin).json()["token_id"]
    assert client.delete(f"/api/v1/tokens/{boot_id}", headers=h2).status_code == 204
    env.bootstrap_token_file.unlink()

    with TestClient(create_app(env)):  # a2 is still an active admin token
        pass
    assert not env.bootstrap_token_file.exists()

    with client.app.state.sessionmaker() as s:  # same DB file; revoke a2 directly
        s.get(ApiToken, second.json()["id"]).revoked_at = utcnow()
        s.commit()
    with TestClient(create_app(env)) as c:
        fresh = read_bootstrap_token(env)
        assert fresh != admin_token
        me = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {fresh}"})
        assert me.json()["scope"] == "admin"
