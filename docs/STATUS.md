# Status

_Last updated: 2026-11-07_

## Phase 0: Skeleton (in progress, code complete)

Done:
- Repo layout: `server/` (FastAPI app), `web/` (React UI), `docker/`, `unraid/`, `docs/`.
- Config from env: `/config`, `/data`, `/import`, Docker socket, `HOST_DATA_PATH`.
- SQLite in WAL mode, SQLAlchemy 2, Alembic migrations run on startup.
- Token auth with `read` / `admin` scopes; tokens stored as sha256 hashes.
- First-run admin token written to `/config/admin-token.txt` (mode 600). It is never logged.
  With no active admin token, the next start writes a new one (recovery path).
- One error envelope everywhere: `{error: {code, message, details}}`, including validation
  errors (input values are stripped from details so secrets can't leak) and unknown `/api` paths.
- Endpoints: `GET /system/health` (no auth), `GET /system/info`, `GET/PATCH /system/settings`,
  `GET /auth/me`, `GET/POST /tokens`, `DELETE /tokens/{id}`.
- Stable `operationId`s (endpoint function names). Spec committed at `docs/openapi.json`.
- Web UI: token login (admin tokens only), Settings (settings, tokens, server capabilities),
  Library and Jobs placeholders. Uses only the generated typed client.
- Multi-stage Dockerfile (python:3.12-slim-trixie + built UI + 7zip), entrypoint drops to
  PUID:PGID with `setpriv`, adds the Docker socket's group when the socket is mounted, never
  chowns `/data` recursively.
- docker-compose for development, Unraid template XML.
- CI: ruff, pytest, OpenAPI/client drift checks, UI build, image build + container smoke test
  (PUID:PGID, file ownership, health, auth, token not in logs), push to GHCR on `main`.

Verified:
- 24 pytest tests pass locally (Python 3.14, Windows).
- Server + built UI run natively; login, settings, token creation checked in a browser.

Not yet verified:
- The Docker image has not been built locally (no Docker on the dev PC). CI builds and
  smoke-tests it.
- Not yet started on the Unraid server. This is the Phase 0 acceptance check.

## Next: Phase 1 (track, download, serve)

- Job queue (DB-backed, asyncio workers, process pool for hashing) and `/jobs` endpoints.
- Recipe loader/validator; built-in recipes for the five test games, researched per
  `docs/games/<id>.md`.
- GitHub source with ETag caching, draft recipe from repo URL, asset-to-platform matching.
- Download, verify (sha256), storage layout, versions, pinning, pruning, scheduler, ranged downloads.
- UI: library, game detail, versions, jobs.

## Open questions

- The GHCR package must be public (or Unraid given registry credentials) before Unraid can pull
  the image. GitHub makes new packages private by default.
