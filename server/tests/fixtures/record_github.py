"""Record GitHub API responses for the five test games as test fixtures.

Run from the repo root (a token avoids the 60 requests/hour limit):

    GITHUB_TOKEN=$(gh auth token) python server/tests/fixtures/record_github.py

Responses are trimmed to the fields the server reads, and to the latest few releases,
so fixtures stay small and diffs stay readable. No game data is involved: these are
release metadata only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

REPOS = [
    "mr-finster/vf2-recomp",
    "HarbourMasters/Shipwright",
    "Awakenchan/LanternEngine",
    "NoRain211/doaxvb-re",
    "kuraeow/ultratears",
]
KEEP_RELEASES = 6
REPO_FIELDS = ["full_name", "name", "description", "html_url", "default_branch", "archived"]
RELEASE_FIELDS = [
    "id",
    "tag_name",
    "name",
    "draft",
    "prerelease",
    "published_at",
    "html_url",
    "body",
]
ASSET_FIELDS = ["id", "name", "size", "digest", "content_type", "browser_download_url"]
OUT = Path(__file__).parent / "github"


def main() -> None:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RecompManager-fixtures"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    OUT.mkdir(exist_ok=True)
    with httpx.Client(base_url="https://api.github.com", headers=headers, timeout=30) as c:
        for repo in REPOS:
            info = c.get(f"/repos/{repo}").raise_for_status().json()
            releases = c.get(f"/repos/{repo}/releases", params={"per_page": 30})
            releases.raise_for_status()
            data = {
                "repo": {k: info.get(k) for k in REPO_FIELDS},
                "releases": [
                    {
                        **{k: r.get(k) for k in RELEASE_FIELDS},
                        "assets": [{k: a.get(k) for k in ASSET_FIELDS} for a in r["assets"]],
                    }
                    for r in releases.json()[:KEEP_RELEASES]
                ],
            }
            name = repo.replace("/", "__").lower() + ".json"
            (OUT / name).write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            print(f"{repo}: {len(data['releases'])} releases -> {name}")


if __name__ == "__main__":
    main()
