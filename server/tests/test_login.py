from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from recompmanager.config import Env
from recompmanager.main import create_app
from recompmanager.models import ApiToken, utcnow
from recompmanager.passwords import LoginThrottle, hash_password, verify_password
from tests.test_auth import assert_error

CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
SAFARI_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


def set_password(client, headers, username="seth", password="correct horse", current=None):
    body = {"username": username, "new_password": password}
    if current is not None:
        body["current_password"] = current
    return client.put("/api/v1/auth/password", json=body, headers=headers)


def login(client, username="seth", password="correct horse", ua=CHROME_WIN):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={"User-Agent": ua},
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_password_hashing_roundtrip():
    h = hash_password("hunter22")
    assert h.startswith("scrypt$") and "hunter22" not in h
    assert verify_password("hunter22", h)
    assert not verify_password("hunter23", h)
    assert not verify_password("x", "garbage")


def test_login_before_password_set(client: TestClient):
    assert_error(login(client), 409, "password_not_set")


def test_set_password_then_login(client: TestClient, admin):
    me = client.get("/api/v1/auth/me", headers=admin).json()
    assert me["password_set"] is False and me["kind"] == "api"

    assert set_password(client, admin).status_code == 204
    r = login(client, username="Seth ")  # username is case- and space-insensitive
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert token.startswith("rms_")

    me = client.get("/api/v1/auth/me", headers=bearer(token)).json()
    assert me == {
        "token_id": me["token_id"],
        "token_name": "Chrome on Windows",
        "scope": "admin",
        "kind": "session",
        "username": "seth",
        "password_set": True,
    }
    # Sessions are admin: they can do everything the UI needs.
    assert client.get("/api/v1/tokens", headers=bearer(token)).status_code == 200


def test_password_rules(client: TestClient, admin):
    assert_error(set_password(client, admin, password="short"), 422, "validation_error")
    assert_error(set_password(client, admin, username=" "), 422, "validation_error")


def test_wrong_credentials(client: TestClient, admin):
    set_password(client, admin)
    assert_error(login(client, password="nope"), 401, "invalid_credentials")
    assert_error(login(client, username="someone"), 401, "invalid_credentials")
    # Response must not reveal which part was wrong.
    a = login(client, password="nope").json()["error"]["message"]
    b = login(client, username="someone").json()["error"]["message"]
    assert a == b


def test_lockout_after_repeated_failures(client: TestClient, admin):
    set_password(client, admin)
    for _ in range(5):
        assert login(client, password="nope").status_code == 401
    r = login(client)  # even the right password is refused while locked out
    assert_error(r, 429, "rate_limited")
    assert r.json()["error"]["details"]["retry_after_seconds"] > 0


def test_throttle_window_and_reset():
    t = LoginThrottle(max_failures=2, window_s=60)
    t.fail("a")
    assert t.retry_after("a") is None
    t.fail("a")
    assert t.retry_after("a") is not None
    assert t.retry_after("b") is None
    t.succeed("a")
    assert t.retry_after("a") is None


def test_sessions_listed_and_revocable(client: TestClient, admin):
    set_password(client, admin)
    pc = login(client).json()["token"]
    phone = login(client, ua=SAFARI_IOS).json()["token"]

    sessions = client.get("/api/v1/auth/sessions", headers=bearer(pc)).json()
    assert {s["name"] for s in sessions} == {"Chrome on Windows", "Safari on iOS"}
    assert [s["current"] for s in sessions if s["name"] == "Chrome on Windows"] == [True]

    # Sessions aren't API tokens, and the two endpoints don't cross over.
    tokens = client.get("/api/v1/tokens", headers=admin).json()
    assert [t["name"] for t in tokens] == ["bootstrap"]
    phone_id = next(s["id"] for s in sessions if s["name"] == "Safari on iOS")
    assert_error(client.delete(f"/api/v1/tokens/{phone_id}", headers=admin), 404, "not_found")

    assert client.delete(f"/api/v1/auth/sessions/{phone_id}", headers=bearer(pc)).status_code == 204
    assert_error(client.get("/api/v1/auth/me", headers=bearer(phone)), 401, "unauthorized")


def test_logout_revokes_session_only(client: TestClient, admin):
    set_password(client, admin)
    token = login(client).json()["token"]
    assert client.post("/api/v1/auth/logout", headers=bearer(token)).status_code == 204
    assert_error(client.get("/api/v1/auth/me", headers=bearer(token)), 401, "unauthorized")
    # Logging out with an API token doesn't revoke it.
    assert client.post("/api/v1/auth/logout", headers=admin).status_code == 204
    assert client.get("/api/v1/auth/me", headers=admin).status_code == 200


def test_change_password_from_session_needs_current(client: TestClient, admin):
    set_password(client, admin)
    mine = login(client).json()["token"]
    other = login(client, ua=SAFARI_IOS).json()["token"]

    r = set_password(client, bearer(mine), password="new password")
    assert_error(r, 403, "wrong_password")
    r = set_password(client, bearer(mine), password="new password", current="wrong")
    assert_error(r, 403, "wrong_password")
    r = set_password(client, bearer(mine), password="new password", current="correct horse")
    assert r.status_code == 204

    # This session survives; other sessions are signed out.
    assert client.get("/api/v1/auth/me", headers=bearer(mine)).status_code == 200
    assert_error(client.get("/api/v1/auth/me", headers=bearer(other)), 401, "unauthorized")
    assert login(client, password="new password").status_code == 200
    assert login(client).status_code == 401


def test_admin_api_token_resets_password_without_current(client: TestClient, admin):
    # Recovery path: `recompmanager token create` gives an API token, which can reset.
    set_password(client, admin)
    assert set_password(client, admin, password="reset password").status_code == 204
    assert login(client, password="reset password").status_code == 200


def test_read_token_cannot_set_password(client: TestClient, reader):
    assert_error(set_password(client, reader), 403, "forbidden")


def test_session_expires_when_idle(client: TestClient, admin):
    set_password(client, admin)
    token = login(client).json()["token"]
    with client.app.state.sessionmaker() as s:
        row = s.query(ApiToken).filter(ApiToken.kind == "session").one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    r = client.get("/api/v1/auth/me", headers=bearer(token))
    assert_error(r, 401, "unauthorized")
    assert "expired" in r.json()["error"]["message"]


def test_no_bootstrap_when_password_set(env: Env, client: TestClient, admin):
    set_password(client, admin)
    boot_id = client.get("/api/v1/auth/me", headers=admin).json()["token_id"]
    session_token = login(client).json()["token"]
    r = client.delete(f"/api/v1/tokens/{boot_id}", headers=bearer(session_token))
    assert r.status_code == 204
    env.bootstrap_token_file.unlink()
    with TestClient(create_app(env)):
        pass
    assert not env.bootstrap_token_file.exists()
