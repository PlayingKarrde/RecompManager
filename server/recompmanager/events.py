"""In-process event bus feeding ``GET /events`` (Server-Sent Events).

Events are hints for live UIs, not a durable log: a subscriber that falls behind
drops events and should re-read state from the API.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)

# Event types. Payloads are small JSON objects; see the /events endpoint docs.
JOB_UPDATED = "job.updated"  # {id, kind, game_id, status, progress, message}
GAME_UPDATED = "game.updated"  # {id}
UPDATE_AVAILABLE = "game.update_available"  # {id, version}


class EventBus:
    def __init__(self, queue_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, type_: str, data: dict[str, Any]) -> None:
        """Safe to call from the event loop or from worker threads."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(type_, data)
        else:
            with contextlib.suppress(RuntimeError):  # loop shutting down
                loop.call_soon_threadsafe(self._deliver, type_, data)

    def _deliver(self, type_: str, data: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            # A full queue means a slow subscriber; it re-syncs from the API.
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait((type_, data))

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[tuple[str, dict[str, Any]]]]:
        q: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(self._queue_size)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)
