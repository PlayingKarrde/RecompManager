"""A thin client for the Recomp Manager REST API."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

CHUNK = 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Api:
    def __init__(self, server: str, token: str, client: httpx.Client | None = None) -> None:
        self.server = server.rstrip("/")
        self.http = client or httpx.Client(timeout=httpx.Timeout(30, read=300))
        self.headers = {"Authorization": f"Bearer {token}"}

    def url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.server}{path}"

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                raise ApiError(r.status_code, err["code"], err["message"])
            except (ValueError, KeyError, TypeError):
                raise ApiError(r.status_code, "http_error", f"HTTP {r.status_code}") from None
        return r

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(self.url(path), params=params or None, headers=self.headers)
        return self._check(r).json()

    def post(self, path: str, body: Any = None) -> Any:
        return self._check(self.http.post(self.url(path), json=body, headers=self.headers)).json()

    def put(self, path: str, body: Any = None) -> Any:
        return self._check(self.http.put(self.url(path), json=body, headers=self.headers)).json()

    # ------------------------------------------------------------ files

    def download(
        self,
        path: str,
        dest: Path,
        *,
        sha256: str | None = None,
        size: int | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> str:
        """Download to ``dest``, resuming from ``dest.part``; returns the sha256.

        A finished file whose hash matches is kept as is.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and sha256 and file_sha256(dest) == sha256:
            return sha256
        part = dest.with_name(dest.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if size is not None and have > size:
            part.unlink()
            have = 0
        h = hashlib.sha256()
        if have:
            with part.open("rb") as f:
                while chunk := f.read(CHUNK):
                    h.update(chunk)
        headers = dict(self.headers)
        if have:
            headers["Range"] = f"bytes={have}-"
        with self.http.stream("GET", self.url(path), headers=headers) as r:
            if r.status_code == 416 and size is not None and have == size:
                pass
            else:
                if r.status_code == 200 and have:
                    have, h = 0, hashlib.sha256()
                    part.unlink(missing_ok=True)
                elif r.status_code not in (200, 206):
                    r.read()
                    self._check(r)
                with part.open("ab") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        if on_progress:
                            on_progress(have, size)
        digest = h.hexdigest()
        if sha256 and digest != sha256:
            part.unlink(missing_ok=True)
            raise ApiError(0, "checksum", f"Checksum mismatch for {dest.name}; try again")
        part.replace(dest)
        return digest


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()
