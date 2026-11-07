# Status

_Last updated: 2026-11-07_

## Phase 0: Skeleton (done, pending the Unraid check)

- Repo layout: `server/` (FastAPI app), `web/` (React UI), `docker/`, `unraid/`, `docs/`.
- Config from env: `/config`, `/data`, `/import`, Docker socket, `HOST_DATA_PATH`.
- SQLite in WAL mode, SQLAlchemy 2, Alembic migrations run on startup.
- Auth: API tokens (`read` / `admin`, stored as sha256) for clients and automation, plus a
  username/password web login (added at Seth's request) that issues 30-day sliding session
  tokens, lists and revokes signed-in browsers, and locks an address out after 5 failures.
  First-run admin token in `/config/admin-token.txt` (never logged).
- One error envelope everywhere: `{error: {code, message, details}}`.
- Stable `operationId`s; spec committed at `docs/openapi.json`; UI uses a generated client.
- Dockerfile (runs as PUID:PGID via `setpriv`), docker-compose, Unraid template, CI (lint,
  tests, spec/client drift, image build + container smoke test, push to GHCR on `main`).

Not yet confirmed: the container running on Seth's Unraid server.

## Phase 1: Track, download, serve (done)

- **Jobs:** DB-backed queue, asyncio workers, one job per game at a time, progress, logs,
  cancel; jobs interrupted by a restart are marked failed. `GET /jobs`, `GET /jobs/{id}`,
  `POST /jobs/{id}/cancel`. Live updates via SSE `GET /events`.
- **Recipes:** YAML in `/config/recipes/<id>.yaml` (Pydantic-validated, hand edits picked up,
  broken files reported per game). Built-in recipes for the five test games, researched from
  their repos (see `docs/games/`). `GET /recipes` lists them.
- **Adding games:** `POST /games {repo_url}` uses the built-in recipe when the repo has one;
  otherwise it creates a **draft** with asset rules guessed from release names. Guessing
  compares names across releases so versions and codenames drop out, survives packaging
  changes, and prefers plain builds over Flatpak/debug ones. Confirm with `PATCH status=active`.
  Adding or confirming fetches releases but doesn't download; the user starts the first download.
- **GitHub:** ETag conditional requests (304s are free), clear rate-limit errors with a hint
  to add a token, pre-release channel support.
- **Downloads:** streamed, resumable (`.part` + Range), sha256 verified against GitHub's
  asset digest, disk-space check, one stored file serving several platforms.
- **Versions:** release notes stored; pin/unpin; activate (rollback/roll forward); delete
  files; pruning keeps `keep_versions` (default 2) plus pinned and current.
- **Serving:** `GET /artifacts/{id}/download` with Range support (HTTP 206).
- **Scheduler:** checks all active games every `update_check_interval_hours` (default 6,
  first run 5 min after start); auto-update games download new releases.
- **Web UI:** library with add-by-URL and built-in quick-add, game detail (status, asset rule
  editor with live match preview, draft confirmation, versions with notes/files, recent jobs
  with progress), jobs page with logs and cancel. Works at phone width.

### Acceptance (Phase 1)

| Criterion | Status |
|---|---|
| All five test games can be added | ✅ Tested against recorded GitHub fixtures; VF2 also added live |
| Assets matched per platform (doaxvb under all its platforms) | ✅ doaxvb now ships 3 zips (one per OS); each is matched |
| Updates detected when a fixture release is published | ✅ `test_check_uses_etag_and_detects_new_release` |
| An API download resumes after interruption | ✅ Tested in pytest, and live with curl (`-r` then `-C -`, 206, sha256 matches) |

Verified live (dev PC, real GitHub): VF2 added, 19 versions found, v0.1.18 downloaded; all four
stored files match GitHub's sha256 digests. A draft from Zelda64Recomp got correct rules for
4 platforms.

## Deviations from CLAUDE.md

- Storage is `/data/library/<game>/<version>/<asset>` (no `<platform>/` level): a file that
  serves several platforms is stored once and the artifacts table lists its platforms.
- doaxvb, Lantern and SoH facts changed since CLAUDE.md was written (see `docs/games/`).

## Next: Phase 2 (inputs and install plans)

- Inputs vault: chunked/resumable upload, import from `/import` (copy/move), verification
  (hashes, `required_paths`, `hashes_url` lists like SoH's), cached folder hashing in the
  process pool, tar streaming + per-file manifest for folder downloads.
- Install-plan engine: turn recipe step templates into concrete steps (artifact ids, sha256,
  sizes), strategy fallback order, platform fallback.
- `rlclient` reference client; client registration.
- Open design points: archives with a versioned top-level folder (doaxvb, UltraTears) need a
  `strip_root` or a known folder name in plans; SoH's macOS `.dmg` needs client support.

## Open questions

- Windows ROM placement for SoH, Lantern's launcher config and save paths (see `docs/games/`).
- Browser downloads of artifacts from the web UI need a short-lived signed URL (bearer headers
  can't be set on a plain link). Not built yet.
