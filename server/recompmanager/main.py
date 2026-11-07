"""Application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from recompmanager import __version__
from recompmanager.api import api_router
from recompmanager.bootstrap import ensure_admin_token
from recompmanager.config import Env
from recompmanager.db import make_engine, make_sessionmaker, run_migrations
from recompmanager.errors import ApiError, install_error_handlers

log = logging.getLogger(__name__)


def _operation_id(route: APIRoute) -> str:
    # Stable operationIds = the endpoint function names. Generated clients rely on these.
    return route.name


def create_app(env: Env | None = None) -> FastAPI:
    env = env or Env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        env.ensure_dirs()
        run_migrations(env.database_url)
        ensure_admin_token(env, app.state.sessionmaker)
        if not env.docker_available:
            log.info("Docker socket not mounted; server-side builds are disabled")
        yield
        app.state.engine.dispose()

    app = FastAPI(
        title="Recomp Manager",
        version=__version__,
        description="Self-hosted library server for game recompilations and ports.",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
        generate_unique_id_function=_operation_id,
        lifespan=lifespan,
    )
    app.state.env = env
    app.state.engine = make_engine(env.database_url)
    app.state.sessionmaker = make_sessionmaker(app.state.engine)

    install_error_handlers(app)
    app.include_router(api_router)

    @app.api_route(
        "/api/{rest:path}",
        include_in_schema=False,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def _api_not_found(rest: str):
        raise ApiError(404, "not_found", "No such API endpoint")

    if env.static_dir and (env.static_dir / "index.html").is_file():
        _mount_frontend(app, env.static_dir)
    else:
        log.info("No built frontend found; serving the API only")

    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    root = static_dir.resolve()
    index = root / "index.html"
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def _spa(path: str):
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
