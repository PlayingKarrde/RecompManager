# Status

_Last updated: 2026-11-07_

## Phase 0: Skeleton (done)

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

## Phase 2: Inputs and install plans (done, pending device tests)

- **Games and recomps** (Seth's request): games are original titles; recomps are projects
  pointing at a game (`game:` block in recipes; guessed from the repo description for
  drafts, confirmed by the user). Inputs belong to the game. A recomp links to one game for
  now; multi-game projects (one recomp, several games) aren't modelled yet.
  Library layout: `/data/library/<game>/<recomp>/<platform>/<version>/<asset>`, one artifact
  per platform, shared files hardlinked (copy fallback). A relocate job moves files after a
  game change and once after upgrading from the Phase 1 layout.
- **Built-in recipe updates**: the server remembers which built-in a library recipe came from.
  Unedited copies are refreshed at startup; edited ones get "Reset to built-in recipe" in the
  UI (`POST /recomps/{id}/reset-recipe`). Copies made before this existed only gain a missing
  `game:` block, then show the reset option.
- **Inputs vault**: file or folder; copied into `/data/inputs` or read in place from
  `/roms`/`/import`; import (copy / move / reference / one zip entry / unpack a zip or 7z),
  resumable upload. Facts: sha1/md5/crc32/sha256, N64 byte order normalised, zip index, PS4
  param.sfo (title ID, version), Switch main.npdm program ID, PS4 .pkg content ID, Xbox ISO
  header; folders get a per-file sha256 manifest, re-hashed only where size/mtime changed.
- **Verification** per recomp and slot: hash lists (SoH's supportedHashes.json via
  `hashes_url`, MQ vs normal via `hash_name_pattern`), required paths, PS4 title ID +
  version, Switch program ID. Result: verified / unverified / mismatch with a reason;
  unverified inputs are allowed with a warning.
- **ROM library scan** (Seth's request): `/roms` and `/import`, recursive, cached by
  path/size/mtime, on demand and every 24 h. Hash-verified matches (and a game's single
  "likely" match) are added automatically by reference; archive entries are extracted into the
  vault. Encrypted .pkg/.nsp/.xci are reported as unusable. Suggestions can be accepted or
  dismissed.
- **Install plans**: `GET /recomps/{id}/install-plan?platform=&version=`, typed steps
  (discriminated union), platform fallback, best input per slot, strategy fallback
  (server_* → manual until Phase 3, manual always offers inputs), `ready`/`problems`/`warnings`.
- **Downloads of inputs**: files with Range; folders as a tar stream or file by file from
  the manifest.
- **Clients**: register, report installed versions, list with out-of-date recomps.
- **rlclient**: login/list/plan/install/launch; every step type; resumable, verified,
  idempotent.
- **Web UI**: library grouped by game; game page (inputs with fit per recomp, scan suggestions,
  import browser, upload); recomp page (game picker, install plan preview); settings (ROM
  library, devices).

### Acceptance (Phase 2)

| Criterion | Status |
|---|---|
| `rlclient install vf2-recomp` launches on Windows, macOS, Steam Deck | ⏳ Installs correctly on Windows from the real v0.1.18 release (layout matches the recipe); launching needs Seth's own vf2.zip on real devices |
| Same for SoH (runtime) and Lantern (folder input) on Windows/Linux | ⏳ Plans and installs work in tests with synthetic inputs; needs real dumps and devices |
| doaxvb and UltraTears return working `client_build` plans | ⏳ Plans are produced (run step asks first; UltraTears uses instructions); not run against real ISOs/dumps |

Tests: 134 (pytest) incl. synthetic dumps (`server/tests/dumps.py`) and rlclient against the
test server. Verified live on the dev PC: migration of a real Phase 1 database, file
relocation, scan → auto-add, install plan, rlclient install of the real VF2 release.

## Deviations from CLAUDE.md

- API: `/recomps` is the project (CLAUDE.md's `/games`), `/games` is the original title.
- `place_input` is the recipe template step; plans turn it into `download_input`.
  `download_artifact` gained `strip_root`.
- doaxvb, Lantern and SoH facts changed since CLAUDE.md was written (see `docs/games/`).

## Next: Phase 3 (server-side builds and extraction)

- Builder framework (sibling containers, resource limits, log streaming, cancel), disabled
  without the Docker socket.
- doaxvb `linux-x64` server build; SoH `server_extract` (.o2r) if feasible; then UltraTears
  Linux and Windows cross-builds.

## Open questions

- Windows ROM placement for SoH, Lantern's launcher config and save paths (see `docs/games/`).
- SoH's macOS release is a `.dmg`; rlclient can't unpack it (macOS plan is manual for now).
- Switch dumps: the game version (1.4.3) can't be confirmed from exefs/romfs alone yet.
- Browser downloads of artifacts/inputs from the web UI need short-lived signed URLs.
- Multi-game recomps (one project, several games) aren't modelled yet.
