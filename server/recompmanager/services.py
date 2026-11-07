"""App-wide services, created at startup and shared by API handlers and jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from recompmanager.config import Env
from recompmanager.events import EventBus
from recompmanager.github import GitHub
from recompmanager.jobs import JobManager
from recompmanager.recipes.store import RecipeStore

if TYPE_CHECKING:
    from recompmanager.scheduler import Scheduler


@dataclass
class Services:
    env: Env
    sessionmaker: sessionmaker[Session]
    events: EventBus
    jobs: JobManager
    http: httpx.AsyncClient
    github: GitHub
    recipes: RecipeStore
    scheduler: Scheduler | None = None


def get_services(request: Request) -> Services:
    return request.app.state.services
