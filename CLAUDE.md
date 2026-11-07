# Recomp Library Server: Handoff

## What you are building

A self-hosted server that runs as a Docker container on Unraid and manages a personal collection of game **recompilations / decompilation ports** ("recomps").

The server:

1. **Tracks** recomp projects by their GitHub repo URL. It checks for new releases and downloads them, either on demand or automatically on a schedule.
2. **Stores** release files for every platform the project ships: Windows, Linux, macOS and Android, on x64 and arm64.
3. **Holds game inputs** in a private vault. A game input is the user's own dump of the original game: a ROM file, a disc ISO or an extracted folder. The server verifies each input against what the recomp needs.
4. **Builds or extracts** game files on the server where that is feasible. Where it is not, the server gives the client the input plus instructions.
5. **Serves everything through a documented REST API**, so that clients for each platform (built later, not in this task) can browse, install, update and launch games. Clients should be thin. The server works out an **install plan** for each game and platform, and the client carries it out.

This is **for personal use only, for one user (Seth)**. Sending game inputs to the user's own clients is intended and allowed.

### Non-goals for this task
- Native clients for each platform. Build only the API, a web UI, and a small **reference CLI client** used to test the API (see Phase 2).
- Hosting or sharing anything publicly, or supporting more than one user.
- Discover/catalog importing (Phase 5, design for it but don't build it yet).

## Environment and constraints

- **Host:** Unraid. Ship a single Docker image plus an Unraid Community Applications template XML.
- **Network:** LAN only for v1. Remote access may later come through Tailscale. That needs no app changes, but **always require API tokens** anyway.
- **Volumes:**
  - `/config`: database, settings, recipes, logs (Unraid appdata)
  - `/data`: library (release files, game inputs, build outputs). This can be very large (see UltraTears below).
  - `/import`: optional, read-only. A share the user drops large dumps into for importing.
  - `/var/run/docker.sock`: optional. Server-side builds are enabled only if it is present.
- Env vars: `PUID`, `PGID`, `UMASK`, `TZ`, `HOST_DATA_PATH`. `HOST_DATA_PATH` is the host-side path of `/data`. Sibling build containers need it because bind mounts resolve on the host, not inside this container. This is a common bug, so handle it explicitly.
- Files can be huge: up to 28 GB outputs, and input folders of 16 GB / 300k files. Everything must **stream** (never load whole files into memory), support **HTTP Range / resumable downloads**, and show progress.

## Tech stack (chosen for speed of development, adjust only with good reason)

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, SQLite in WAL mode.
- **Jobs:** a job queue stored in the database and run by asyncio workers inside the same process. No Redis. CPU-heavy work (hashing, extraction) runs in a process pool. Each job has a status, progress, a log, and can be cancelled.
- **Scheduler:** APScheduler, for update checks on a configurable interval (default every 6 hours).
- **HTTP:** `httpx` with GitHub conditional requests (ETag / If-None-Match). Use an optional GitHub token from settings, because unauthenticated access is limited to 60 requests per hour.
- **Archives:** `zipfile`, `tarfile`, the `7z` binary for .7z files. AppImages and APKs are stored as-is.
- **Builds:** Docker SDK for Python, running one builder image per recipe as a sibling container.
- **Frontend:** React + Vite + TypeScript, using a typed client generated from the OpenAPI spec (`openapi-typescript`). The web UI must use **only the public API**, which proves the API is complete for future clients.
- **Image:** multi-stage Dockerfile. The final image is python-slim plus the built static frontend, and the container runs as PUID:PGID.
- **Tests:** pytest. Record GitHub API responses as test fixtures. **Never use real game files in tests or CI.** Use dummy inputs with test hashes.

Future clients are expected to be C#/Avalonia, possibly a fork of Quiver Launcher (MIT; https://github.com/tgeorgiadis/quiver-launcher). Keep the API easy to generate clients from: stable `operationId`s, no polymorphic payloads without a discriminator, and the same error envelope everywhere.

## Core concepts

### Platform keys
`windows-x64`, `windows-arm64`, `linux-x64`, `linux-arm64`, `macos-x64`, `macos-arm64`, `macos-universal`, `android-arm64`, plus an optional variant suffix such as `linux-x64-steamdeck`. Clients ask for their most specific key and the server falls back (`linux-x64-steamdeck` → `linux-x64`, `macos-arm64` → `macos-universal`).

### Recipe (one per game)
A YAML file in `/config/recipes/<id>.yaml`. It is the declarative definition of a game, while the database holds state. Ship the five test games below as built-in recipes. "Add by repo URL" creates a **draft recipe**, guessing asset rules from the latest release's asset names, which the user confirms in the UI.

```yaml
id: vf2-recomp
name: Virtua Fighter 2 Recomp
source:
  type: github            # design for gitlab/codeberg later
  repo: mr-finster/vf2-recomp
  channel: stable         # stable | prerelease
  auto_update: true
assets:                   # regex matched against release asset names
  windows-x64:         { match: 'VirtuaFighter2Recomp-windows\.zip$' }
  macos-universal:     { match: 'VirtuaFighter2Recomp-mac\.zip$' }
  linux-x64-steamdeck: { match: 'VirtuaFighter2Recomp-deck\.tar\.gz$' }
input:
  kind: file              # none | file | folder | iso
  filename: vf2.zip
  description: "MAME `vf2` set, version 2.1"
  verify:                 # any of: hashes (sha1/md5/crc32), required_paths, custom check
    sha1: []              # TODO fill with known-good hashes
platforms:                # strategy PER PLATFORM
  default:
    strategy: runtime     # runtime | server_extract | server_build | client_build | manual
    steps:                # install-plan template (see below)
      - download_artifact: { to: '{install}' }
      - write_file: { path: '{install}/portable.txt', content: '' }
      - place_input: { to: '{install}/roms/vf2.zip' }
saves:
  default: '{install}'    # portable mode keeps saves in install dir
launch:
  windows-x64: { exe: 'VirtuaFighter2Recomp.exe' }   # TODO confirm names
```

### Game-input strategies
| Strategy | Meaning | Server does | Client does |
|---|---|---|---|
| `runtime` | Recomp reads the input at run time | store + verify input | download release + input, place input where expected |
| `server_extract` | One-off extraction produces game data that works on any platform | run extractor in builder container, store output pack | download release + pack |
| `server_build` | Game must be compiled with the input | build in a builder container for that platform | download built output |
| `client_build` | Build is only possible on the target OS | nothing beyond storage | download release + input, run build command / show steps |
| `manual` | No automation possible | store input | show markdown instructions + offer files |

If a better strategy fails or isn't supported for a platform, fall back in this order: `server_build`/`server_extract` → `client_build` → `runtime` → `manual`. The server always offers the input file as the last fallback.

### Install plan (key API design)
`GET /api/v1/recomps/{id}/install-plan?platform=...&version=...` returns an ordered list of **steps** using a small, fixed set of types. Clients only need to implement these:

- `download_artifact {artifact_id, sha256, size, to, extract: bool, strip_root: bool}` (`strip_root` drops a single top-level folder when unpacking)
- `download_input {input_id, sha256, to}` (a file, or a folder delivered as a tar stream / per-file manifest)
- `write_file {path, content}`
- `chmod {path, mode}`
- `run {command, cwd}`, for `client_build`. The client must ask the user to confirm before running it.
- `instructions {markdown}`
- plus metadata: `launch {exe, args, env}`, `saves {path}`, `version`

Placeholders: `{install}`, `{data}` (per-user app-data folder), `{home}`. The client substitutes them. Every step is safe to run twice.

### Inputs vault
- Ways to add an input: browser upload (chunked/resumable, for small files), or **import from a path under `/import`** (the main route for large ISOs and folders, copied or moved into `/data/inputs`).
- Verification: file hashes; for folders, required paths (e.g. `eboot.bin`) and version checks where possible. Show the result as one of `verified` / `unverified` / `mismatch`. **Allow unverified inputs with a warning.**
- Hashing a 300k-file folder is slow. Do it in a background job, cache the result, and only re-hash when file mtimes or sizes change.
- Inputs are never exposed without a token and never logged.

### Versions and storage
```
/data/library/<game>/<version>/<platform>/<original-asset>   # keep originals; don't pre-extract
/data/inputs/<game>/<input-id>/...
/data/builds/<game>/<version>/<platform>/...
```
- Record sha256 and size for every stored file, and verify after download.
- Keep the last N versions (default 2). Pinned versions are never pruned. Rollback means repointing to an older version.
- Store release notes (the changelog) with each version.

## API outline (`/api/v1`, OpenAPI at `/api/v1/openapi.json`)
- Auth: `Authorization: Bearer <token>`. Tokens are created in the UI and have a scope of `read` (clients) or `admin` (UI/automation).
- Web login: `POST /auth/login {username, password}` (single user, set in Settings) returns an admin **session** token that expires after 30 days unused. `GET/DELETE /auth/sessions` lists and signs out browsers. Login is the only unauthenticated endpoint besides health, and it's rate-limited per address.
- **Naming (changed in Phase 2):** a *game* is the original title (Ocarina of Time); a *recomp* is a project that runs it (Ship of Harkinian). Inputs belong to games.
- `GET/POST /recomps`, `GET/PATCH/DELETE /recomps/{id}`. `POST /recomps` takes `{repo_url}` (or a built-in `recipe_id`) and returns a draft recipe when no built-in exists. `PATCH` can set `game_id`/`game_title`. `POST /recomps/{id}/reset-recipe`.
- `GET /games`, `GET/PATCH/DELETE /games/{id}`
- `POST /recomps/{id}/check` and `POST /recomps/{id}/update` (both enqueue jobs).
- `GET /recomps/{id}/versions`, `POST/DELETE /recomps/{id}/versions/{v}/pin`, `POST /recomps/{id}/versions/{v}/activate`, `DELETE /recomps/{id}/versions/{v}`
- `GET /recomps/{id}/versions/{v}/artifacts` and `GET /artifacts/{id}/download` (Range support)
- `GET /games/{id}/inputs`, `POST /games/{id}/inputs/import {path, mode, member, extract}`, `POST /games/{id}/uploads` + `PUT/GET /uploads/{id}` + `POST /uploads/{id}/complete`, `GET /inputs/{id}`, `GET /inputs/{id}/download`, `GET /inputs/{id}/manifest`, `GET /inputs/{id}/files/{path}`, `POST /inputs/{id}/rehash`, `DELETE /inputs/{id}`
- `POST /system/scan`, `GET /games/{id}/suggestions`, `POST /suggestions/{id}/accept|dismiss` (ROM library scan of `/roms` and `/import`)
- `POST /recomps/{id}/builds {version, platform}`, `GET /builds/{id}`, `GET /builds/{id}/log` (SSE) (Phase 3)
- `GET /recomps/{id}/install-plan?platform=&version=`
- `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`
- `GET /events` (SSE: update available, job progress, build done)
- `POST /clients/register`, `PUT /clients/{id}/installed` (the client reports installed versions, so the UI can show which devices are out of date), `GET /clients`.
- `GET /system/health`, `GET/PATCH /system/settings`

Errors use one shape everywhere: `{error: {code, message, details}}`.

## Test-case games (verify every fact; projects change fast, facts as of 2026-11-07)

1. **Ship of Harkinian** (https://github.com/harbourmasters/shipwright). Ocarina of Time port.
   - **Input:** an OoT ROM (file). Seed `input.verify.sha1` from the repo's `docs/supportedHashes.json`. Fetch it at runtime/refresh, don't hardcode it. Input can be **one or two ROMs**: a normal OoT ROM produces `oot.o2r`, and a Master Quest ROM produces `oot-mq.o2r`. Support multiple input slots per game (`input.slots: [oot, oot-mq]`, with mq optional).
   - **How each platform takes the ROM:**
     - Linux: put the ROM in the same folder as `soh.appimage` and run it (`chmod +x`).
     - macOS: `soh.app` asks for the ROM with a file picker, and the `.o2r` goes to `~/Library/Application Support/com.shipofharkinian.soh/`.
     - Windows: launch `soh.exe`. Placement isn't documented, but the `.o2r` is written next to `soh.exe`.
   - **Generated data:** the first launch generates `oot.o2r` / `oot-mq.o2r` next to the executable. The Switch instructions say to generate these on PC and copy them over, so **the `.o2r` files work on any platform**. That makes SoH the prime `server_extract` case.
   - **Strategy:**
     - Phase 2 is `runtime`. The client places the ROM next to the executable, or, better, places an already-generated `.o2r` if the server has one.
     - Phase 3 is `server_extract`. No CLI/headless mode is documented, so try running the Linux AppImage under `xvfb-run` in a builder container with the ROM next to it, wait for the `.o2r` to appear, then kill the process. Also look in the source for the extractor (ZAPD/OTRExporter or similar) and whether it can be called directly. Store the `.o2r` as a game-data pack that install plans place at `{install}/` (Windows/Linux) or the macOS Application Support path above.
   - **Release assets:** the README shows no release asset names. Nightly CI builds are `soh-windows.zip`, `soh-mac.zip`, `soh-linux.zip`. Read the asset names from the actual Releases page when building the recipe, and note that releases are codenamed.
   - **Config:** `shipofharkinian.json` (next to the executable on Windows/Linux). Mods go in a `mods/` folder as `.otr`/`.o2r` archives, which is a future mod-support hook.
   - **Research:** save file location for each platform (not documented in the README), the Windows ROM placement, architectures (arm64?), and whether an Android port exists elsewhere in the HarbourMasters org.
2. **Lantern Engine** (Awakenchan/LanternEngine). Bloodborne (PS4).
   - Input: **folder**. An extracted dump of CUSA03173 at update 1.09, containing `eboot.bin`.
   - Assets: `LanternEngine-<version>-win64.zip`, `LanternEngine-<version>-linux-x64.tar.gz`. Linux is beta and needs Vulkan, Python 3 and glibc ≥ 2.38.
   - Strategy: `runtime` with a folder input. Verify that `eboot.bin` and `sce_sys/param.sfo` exist, and parse the version from `param.sfo` if feasible.
   - **Research:** how the launcher stores the chosen game folder (a config file you could write?), and where saves are kept. If the folder path can't be pre-set, add an `instructions` step.
3. **doaxvb-re** (NoRain211/doaxvb-re). Dead or Alive Xtreme Beach Volleyball (Xbox).
   - Input: the **USA Xbox ISO**.
   - The release ZIP contains only tools and source. **The game must be built locally.** On Windows that's `BuildGame.cmd <iso>` (llvm-mingw, downloaded on first run, ~715 MB). On macOS/Linux it's `./build_game.sh <iso>` (see `docs/building.md`).
   - Strategy: `server_build` for `linux-x64` in a builder container. **Research** whether the Windows build can be cross-compiled on Linux (llvm-mingw has Linux hosts); otherwise use `client_build` on Windows. macOS is `client_build`.
   - **Research:** save locations (`docs/`). Upstream advises building into a new folder on update and keeping the old install and saves, so model updates the same way.
4. **VF2 Recomp** (mr-finster/vf2-recomp). Virtua Fighter 2 (Model 2 arcade). **The easiest case: build this first.**
   - Input: the MAME `vf2.zip` set, version 2.1. It is read at run time; nothing is built.
   - Assets: `VirtuaFighter2Recomp-mac.zip` (Apple silicon and Intel), `-windows.zip`, `-deck.tar.gz` (x86-64 Linux).
   - Portable mode: a `portable.txt` file next to the executable keeps settings, saves and `roms/vf2.zip` in the install folder. Alternatives are `--roms <folder>` or the `VF2_ROMS` environment variable.
   - Strategy: `runtime`, using the recipe example above.
5. **UltraTears** (kuraeow/ultratears). Tears of the Kingdom (Switch). **The hardest case: do it last.**
   - Input: a **folder** containing `exefs/` + `romfs/` at version 1.4.3 (~16 GB, ~304k files).
   - Building takes ~30 minutes and needs 16 GB RAM and ~40 GB free disk. The launcher's "Prepare the game" button, or `python tools/setup.py --dump <path>`, does the build.
   - Assets: `UltraTears-<version>-windows-x86_64.zip`, `UltraTears.AppImage`. A Steam Deck tar of ~28 GB is produced locally.
   - Runtime needs Linux kernel ≥ 6.7, glibc ≥ 2.34 and Vulkan 1.3.
   - Strategy: research a `server_build` for `linux-x64` / Steam Deck (check that the server has the RAM and disk, and refuse with a clear error if not). Windows is `client_build`.
   - Saves are in `userdata/` in the game folder and survive updates. Never overwrite it on update.

Treat these five as acceptance fixtures. Write down anything you learn while researching them in `docs/games/<id>.md`.

## Phases and acceptance criteria

**Phase 0: Skeleton**
- Repo layout, Dockerfile, docker-compose for development, Unraid template XML, settings, token auth, health endpoint, Alembic, CI that runs lint and tests.
- Done when: the container starts on Unraid as PUID:PGID with the volumes above and the web UI loads behind token login.

**Phase 1: Track, download, serve**
- Add a game from a repo URL (draft recipe with asset rules guessed and editable), check for releases (with ETags), download and verify, sort assets into platform keys, version history with release notes, pinning, rollback, pruning, scheduled automatic updates, ranged downloads.
- Web UI: library, game detail, versions, jobs, settings.
- Done when: all five test games can be added. The assets for each platform are matched correctly (doaxvb's "release ZIP" is stored under all of its supported platforms). Updates are detected when a fixture release is published. A download through the API resumes after being interrupted.

**Phase 2: Inputs and install plans**
- Inputs vault (upload plus `/import`), verification, input download (a folder streams as tar, plus a per-file manifest), install-plan engine, a **reference CLI client** (`rlclient`, Python) that installs a game from an install plan on Windows/Linux/macOS, and client registration.
- Done when: `rlclient install vf2-recomp` produces a game that launches on Windows, macOS and Steam Deck. The same works for SoH (runtime) and Lantern (folder input) on Windows/Linux. doaxvb and UltraTears return `client_build` plans that work.

**Phase 3: Server-side builds and extraction**
- Builder framework: a builder Dockerfile per recipe under `/config/recipes/<id>/builder/`, sibling containers with resource limits, live log streaming, cancellation, and outputs stored as artifacts. Disabled with a clear message if the Docker socket isn't mounted.
- Done when: the doaxvb `linux-x64` build runs on the server and installs on Steam Deck, and SoH `server_extract` works if research shows it's feasible. After that, UltraTears Linux and the Windows cross-builds, if feasible.

**Phase 4: Quality of life**
- Update notifications through Apprise URLs.
- Artwork and metadata (SteamGridDB API key optional).
- Save paths exposed per game and platform, for integration with the user's Syncthing-based save sync.
- Install plans emit Steam shortcut metadata for Steam Deck clients.

**Phase 5: Discover (design for it now, build it later)**
- Pluggable `CatalogSource` importers that turn external catalog entries into draft recipes.
- Candidates:
  - The Quiver Launcher catalog (quiverlauncher.com). Its `apps.json` format has `name`, `repository` and `folderName`.
  - RecompLauncher (recomplauncher.com). Currently a website only with no known API, so scraping would be fragile.
- Consider exporting a Quiver-compatible `apps.json` feed.
- Mods (Thunderstore/GameBanana) come after this.

## Security notes
- Builds run code from third-party repos. Run them only in sibling containers with no network after dependencies are fetched where possible, a read-only input mount, and CPU/RAM limits. Never run them in the main process.
- Mounting the Docker socket is effectively root on the host. Document this in the Unraid template description and keep it optional.
- No endpoint without auth except `/system/health` and `POST /auth/login`. Don't log tokens or input paths at info level.

## Working conventions
- Get each phase passing before starting the next. Keep `docs/STATUS.md` updated with what's done, what's next and any open questions.
- Fill in the recipes' `TODO`s from real research. Don't invent hashes, executable names or paths. If you can't confirm something, mark it `unverified` in the recipe and in `docs/games/<id>.md`.
- Commit in small steps with descriptive messages.
