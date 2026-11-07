"""The single user's web login: username + scrypt password hash, stored in settings."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from pydantic import BaseModel
from sqlalchemy.orm import Session

from recompmanager.models import Setting

_KEY = "auth"
# scrypt cost: ~50 ms and 16 MiB per check. Slow enough to blunt guessing, fine for one user.
_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024


class Credentials(BaseModel):
    username: str
    password_hash: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# Verifying against this when the username is wrong keeps response times equal,
# so timing doesn't reveal whether a username exists.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def load(session: Session) -> Credentials | None:
    row = session.get(Setting, _KEY)
    return Credentials.model_validate(row.value) if row else None


def save(session: Session, username: str, password: str) -> None:
    creds = Credentials(username=username, password_hash=hash_password(password))
    row = session.get(Setting, _KEY)
    if row is None:
        session.add(Setting(key=_KEY, value=creds.model_dump()))
    else:
        row.value = creds.model_dump()


def check(session: Session, username: str, password: str) -> bool:
    creds = load(session)
    if creds is None or not hmac.compare_digest(
        creds.username.casefold().encode(), username.strip().casefold().encode()
    ):
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, creds.password_hash)


class LoginThrottle:
    """In-memory lockout: too many failures from one address blocks it for a while."""

    def __init__(self, max_failures: int = 5, window_s: float = 15 * 60) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures.setdefault(key, deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may try again, or None if it may try now."""
        now = time.monotonic()
        with self._lock:
            q = self._recent(key, now)
            if len(q) < self.max_failures:
                return None
            return max(1, int(self.window_s - (now - q[0])) + 1)

    def fail(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._recent(key, now).append(now)

    def succeed(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
