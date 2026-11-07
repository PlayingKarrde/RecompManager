"""GitHub releases source, with conditional requests (ETag / If-None-Match).

A 304 Not Modified reply doesn't count against GitHub's rate limit, so checks of
unchanged repos are free. Without a token GitHub allows 60 requests per hour.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session, sessionmaker

from recompmanager import __version__
from recompmanager.models import HttpCache, utcnow

API = "https://api.github.com"
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


class SourceError(Exception):
    """A problem talking to the release source. The message is user-facing."""


def parse_repo(value: str) -> str:
    """``https://github.com/owner/name(.git)(/…)`` or ``owner/name`` → ``owner/name``."""
    v = value.strip()
    if "://" not in v and v.lower().startswith("github.com/"):
        v = "https://" + v
    if "://" in v:
        u = urlparse(v)
        if u.hostname not in ("github.com", "www.github.com"):
            raise ValueError("Only GitHub repositories are supported for now")
        parts = [p for p in u.path.split("/") if p]
    else:
        parts = [p for p in v.split("/") if p]
    if len(parts) < 2:
        raise ValueError("Expected a GitHub repository URL like https://github.com/owner/name")
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not (_REPO_PART.match(owner) and _REPO_PART.match(name)):
        raise ValueError("That doesn't look like a GitHub repository")
    return f"{owner}/{name}"


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def normalize_release(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": r["tag_name"],
        "name": r.get("name") or r["tag_name"],
        "published_at": _parse_time(r.get("published_at")),
        "prerelease": bool(r.get("prerelease")),
        "draft": bool(r.get("draft")),
        "notes": r.get("body") or "",
        "url": r.get("html_url"),
        "assets": [
            {
                "id": a.get("id"),
                "name": a["name"],
                "size": a.get("size") or 0,
                "digest": a.get("digest"),
                "url": a["browser_download_url"],
            }
            for a in r.get("assets") or []
        ],
    }


class GitHub:
    def __init__(
        self,
        client: httpx.AsyncClient,
        factory: sessionmaker[Session],
        token: Callable[[], str | None],
    ) -> None:
        self.client = client
        self.sessionmaker = factory
        self.token = token

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"RecompManager/{__version__}",
        }
        if tok := self.token():
            h["Authorization"] = f"Bearer {tok}"
        return h

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = str(httpx.URL(API + path, params=params))
        with self.sessionmaker() as s:
            cached = s.get(HttpCache, url)
            etag, body = (cached.etag, cached.body) if cached else (None, None)
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        try:
            resp = await self.client.get(url, headers=headers, timeout=30)
        except httpx.HTTPError as e:
            raise SourceError(f"Couldn't reach GitHub: {e.__class__.__name__}") from e

        if resp.status_code == 304 and body is not None:
            return body
        if resp.status_code == 404:
            raise SourceError("GitHub says this repository doesn't exist (or it's private)")
        if resp.status_code in (403, 429) and (
            resp.headers.get("x-ratelimit-remaining") == "0" or resp.status_code == 429
        ):
            reset = resp.headers.get("x-ratelimit-reset")
            when = (
                datetime.fromtimestamp(int(reset), UTC).strftime("%H:%M UTC") if reset else "later"
            )
            hint = "" if self.token() else " Add a GitHub token in Settings to raise the limit."
            raise SourceError(f"GitHub's rate limit was reached; it resets at {when}.{hint}")
        if resp.status_code == 401:
            raise SourceError("GitHub rejected the token in Settings")
        if resp.status_code >= 400:
            raise SourceError(f"GitHub returned HTTP {resp.status_code}")

        data = resp.json()
        new_etag = resp.headers.get("etag")
        with self.sessionmaker() as s:
            row = s.get(HttpCache, url)
            if row is None:
                s.add(HttpCache(url=url, etag=new_etag, body=data, fetched_at=utcnow()))
            else:
                row.etag, row.body, row.fetched_at = new_etag, data, utcnow()
            s.commit()
        return data

    async def repo(self, repo: str) -> dict[str, Any]:
        return await self.get_json(f"/repos/{repo}")

    async def releases(self, repo: str) -> list[dict[str, Any]]:
        """Published releases, newest first. Drafts are left out."""
        data = await self.get_json(f"/repos/{repo}/releases", {"per_page": 30})
        rels = [normalize_release(r) for r in data if not r.get("draft")]
        epoch = datetime.min.replace(tzinfo=UTC)
        rels.sort(key=lambda r: r["published_at"] or epoch, reverse=True)
        return rels


def newest_on_channel(releases: list[dict[str, Any]], channel: str) -> dict[str, Any] | None:
    for r in releases:  # newest first
        if channel == "prerelease" or not r["prerelease"]:
            return r
    return None
