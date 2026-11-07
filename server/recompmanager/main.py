"""Application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from recompmanager import __version__, app_settings
from recompmanager import inputs as _inputs  # noqa: F401  (registers job handlers)
from recompmanager import scan as _scan  # noqa: F401
from recompmanager.api import api_router
from recompmanager.bootstrap import ensure_admin_token
from recompmanager.builder import DockerBackend  # also registers the build job handler
from recompmanager.config import Env
from recompmanager.db import make_engine, make_sessionmaker, run_migrations
from recompmanager.errors import ApiError, install_error_handlers
from recompmanager.events import EventBus
from recompmanager.github import GitHub
from recompmanager.jobs import JobManager
from recompmanager.library import reconcile_all  # also registers job handlers
from recompmanager.passwords import LoginThrottle
from recompmanager.recipes.store import RecipeStore
from recompmanager.scheduler import Scheduler
from recompmanager.services import Services

log = logging.getLogger(__name__)


def _operation_id(route: APIRoute) -> str:
    # Stable operationIds = the endpoint function names. Generated clients rely on these.
    return route.name


def create_app(
    env: Env | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    builder_backend: Any = None,
) -> FastAPI:
    """``http_transport`` and ``builder_backend`` stand in for outgoing HTTP (GitHub,
    downloads) and Docker in tests."""
    env = env or Env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        env.ensure_dirs()
        run_migrations(env.database_url)
        factory = app.state.sessionmaker
        ensure_admin_token(env, factory)
        if not env.docker_available:
            log.info("Docker socket not mounted; server-side builds are disabled")

        bus = EventBus()
        bus.bind(asyncio.get_running_loop())
        http = httpx.AsyncClient(transport=http_transport, follow_redirects=True)

        def github_token() -> str | None:
            with factory() as s:
                return app_settings.load(s).github_token

        svc = Services(
            env=env,
            sessionmaker=factory,
            events=bus,
            jobs=None,  # type: ignore[arg-type]  # set just below; jobs need services
            http=http,
            github=GitHub(http, factory, github_token),
            recipes=RecipeStore(env.recipes_dir),
            builder=builder_backend or DockerBackend(env),
        )
        svc.jobs = JobManager(factory, bus, services=svc, workers=env.job_workers)
        app.state.services = svc
        await svc.jobs.start()
        with factory() as s:
            reconcile_all(s, svc.recipes, svc.jobs)
        if env.scheduler:
            svc.scheduler = Scheduler(svc)
            svc.scheduler.start()
        try:
            yield
        finally:
            if svc.scheduler:
                svc.scheduler.stop()
            await svc.jobs.stop()
            await http.aclose()
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
    app.state.login_throttle = LoginThrottle()

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

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def _spa(path: str):
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
