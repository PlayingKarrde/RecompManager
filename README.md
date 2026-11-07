# Recomp Manager

A self-hosted server, run as a Docker container on Unraid, that manages a personal library of
game recompilations and decompilation ports. It tracks each project's GitHub releases, stores the
builds for every platform, keeps your own game dumps in a private vault, and serves install plans
to thin clients over a token-protected REST API.

Personal use, single user. See [CLAUDE.md](CLAUDE.md) for the full design and
[docs/STATUS.md](docs/STATUS.md) for progress.

## Run on Unraid

1. Add the template: Docker → Add Container → Template repositories, or copy
   [`unraid/recompmanager.xml`](unraid/recompmanager.xml) to
   `/boot/config/plugins/dockerMan/templates-user/`.
2. Set **Library** (`/data`) and **HOST_DATA_PATH** to the same host path.
3. Start it, open the web UI, and log in with the token in
   `/mnt/user/appdata/recompmanager/admin-token.txt`. Create your own tokens under Settings, then
   delete that file.

Lost every admin token? Revoked tokens can't be restored, but restarting the container with no
active admin token writes a fresh `admin-token.txt`. You can also run:

```bash
docker exec RecompManager recompmanager token create --name rescue --scope admin
```

### Volumes and variables

| Path / var | Purpose |
|---|---|
| `/config` | Database, settings, recipes, logs |
| `/data` | Release files, game inputs, build outputs (large) |
| `/import` | Optional, read-only. Drop big dumps here to import them |
| `/var/run/docker.sock` | Optional. Enables server-side builds. **Root-equivalent on the host** |
| `PUID` / `PGID` / `UMASK` | Run-as user (Unraid default 99:100) |
| `HOST_DATA_PATH` | Host path of `/data`; build containers mount from it |
| `TZ` | Time zone |

## API

OpenAPI spec: `/api/v1/openapi.json` (also committed at [`docs/openapi.json`](docs/openapi.json)).
Interactive docs: `/api/v1/docs`. Every request except `GET /api/v1/system/health` needs
`Authorization: Bearer <token>`. Errors always look like
`{"error": {"code": "...", "message": "...", "details": ...}}`.

## Development

Needs Python 3.12+ and Node 24.

```bash
python -m venv .venv
.venv/bin/pip install -e "server[dev]"      # Windows: .venv\Scripts\pip
cd server && pytest && ruff check . && cd ..
```

Run the server natively (data goes in `./dev-config` and `./dev-data`):

```bash
RM_CONFIG_DIR=dev-config RM_DATA_DIR=dev-data RM_STATIC_DIR=web/dist .venv/bin/recompmanager serve
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
