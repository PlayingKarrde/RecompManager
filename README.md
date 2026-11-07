# Recomp Manager

A self-hosted server, run as a Docker container on Unraid, that manages a personal library of
game recompilations and decompilation ports. It tracks each project's GitHub releases, stores the
builds for every platform, keeps your own game dumps in a private vault, and serves install plans
to thin clients over a token-protected REST API.

Personal use, single user. See [CLAUDE.md](CLAUDE.md) for the full design and
[docs/STATUS.md](docs/STATUS.md) for progress.

## Run on Unraid

1. Create a share for the library, e.g. `recomps` (and optionally `recomp-import`).
2. Download the template from the Unraid terminal:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-RecompManager.xml https://raw.githubusercontent.com/PlayingKarrde/RecompManager/main/unraid/recompmanager.xml
   ```
3. Docker → Add Container → pick **RecompManager** under *User templates*. Set **Library**
   (`/data`) and **HOST_DATA_PATH** to the same host path. The WebUI port can be any free host
   port; the container always listens on 8080 inside.
4. Open the web UI, choose **Sign in with a token instead**, and paste the `rm_…` line from
   `/mnt/user/appdata/recompmanager/admin-token.txt`.
5. Settings → Account: set a username and password. From then on, sign in with those from any
   browser. Revoke the `bootstrap` token and delete `admin-token.txt`.

### Signing in

- **Browsers** sign in with the username and password. Each browser gets a session that lasts
  until you sign it out or it goes 30 days unused; Settings lists them. Five failed attempts from
  one address lock it out for 15 minutes.
- **Game clients and scripts** use API tokens created under Settings → API tokens: `read` for
  clients, `admin` for automation. They don't expire; revoke one to cut off a lost device.

Forgot the password? Create an admin token in the container, sign in with it, and set a new
password under Settings → Account (no current password needed with an API token):

```bash
docker exec RecompManager recompmanager token create --name rescue --scope admin
```

If there's no password set and no active admin API token, the next start writes a fresh
`admin-token.txt`.

### Volumes and variables

| Path / var | Purpose |
|---|---|
| `/config` | Database, settings, recipes, logs |
| `/data` | Release files, game inputs, build outputs (large) |
| `/roms` | Optional, read-only. Your ROM/dump library; scanned to find each game's inputs |
| `/import` | Optional, read-only. Drop big dumps here to import them |
| `/var/run/docker.sock` | Optional. Enables server-side builds. **Root-equivalent on the host** |
| `PUID` / `PGID` / `UMASK` | Run-as user (Unraid default 99:100) |
| `HOST_DATA_PATH` | Host path of `/data`; build containers mount from it |
| `TZ` | Time zone |

## How it's organised

**Games** are the original titles (Ocarina of Time). **Recomps** are projects that run them
(Ship of Harkinian), tracked from their GitHub releases. **Inputs** are your own dumps of a
game; every recomp of that game can use them. Files live in
`/data/library/<game>/<recomp>/<platform>/<version>/` and `/data/inputs/<game>/`.

Inputs arrive three ways:

- **ROM library scan** (`/roms`, plus `/import`): finds ROMs by checksum (also inside zips,
  in any N64 byte order), PS4 and Switch dump folders by title ID, and files a recipe names.
  Confident matches are added automatically and read in place; nothing in `/roms` is changed.
- **Import** a file or folder from `/import` or `/roms` (use in place, copy, or unpack a zip).
- **Upload** a file from the browser (resumable).

**Server builds:** with the Docker socket mounted, the server builds or extracts what it can
by itself (Ship of Harkinian's `.o2r` from your ROM, doaxvb and UltraTears for Linux / Steam
Deck) as soon as a new version and a usable input are both there. Clients then download the
result instead of the ROM. Without the socket, clients get a fallback plan.

Clients ask for an **install plan** (`GET /api/v1/recomps/{id}/install-plan?platform=`) and
carry out its steps.

## Reference client (rlclient)

```bash
pip install ./client
rlclient login http://tower:8095        # paste a read token from Settings
rlclient list
rlclient install vf2-recomp             # into ~/Games/vf2-recomp
rlclient launch vf2-recomp
```

## API

OpenAPI spec: `/api/v1/openapi.json` (also committed at [`docs/openapi.json`](docs/openapi.json)).
Interactive docs: `/api/v1/docs`. Every request except `GET /api/v1/system/health` needs
`Authorization: Bearer <token>`. Errors always look like
`{"error": {"code": "...", "message": "...", "details": ...}}`.

## Development

Needs Python 3.12+ and Node 24.

```bash
python -m venv .venv
.venv/bin/pip install -e "server[dev]" -e client      # Windows: .venv\Scripts\pip
cd server && pytest && ruff check . && cd ..
```

Run the server natively (data goes in `./dev-config` and `./dev-data`):

```bash
RM_CONFIG_DIR=dev-config RM_DATA_DIR=dev-data RM_ROMS_DIR=dev-roms RM_STATIC_DIR=web/dist .venv/bin/recompmanager serve
```

Web UI with hot reload (proxies `/api` to `:8080`):

```bash
cd web && npm install && npm run dev
```

After changing the API, regenerate the spec and the typed client (CI fails if they're stale):

```bash
.venv/bin/recompmanager openapi -o docs/openapi.json && (cd web && npm run gen:api)
```

Or run the whole container with `docker compose up --build`.

Never commit real game files. Tests use dummy inputs only.
