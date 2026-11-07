from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from recompmanager.auth import Principal, require_token
from recompmanager.services import Services, get_services

router = APIRouter(tags=["events"])
KEEPALIVE_S = 15


@router.get(
    "/events",
    summary="Live events (Server-Sent Events)",
    description=(
        "A `text/event-stream` of hints for live UIs. Each event has a type and a JSON payload:\n\n"
        "- `job.updated`: `{id, kind, game_id, status, progress, message}`\n"
        "- `game.updated`: `{id}`: re-read the game\n"
        "- `game.update_available`: `{id, version}`\n\n"
        "Events can be dropped when a client is slow; re-read state from the API after "
        "reconnecting. Browsers can't set headers on EventSource, so read the stream with "
        "fetch() and the Authorization header."
    ),
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Event stream"}},
)
async def stream_events(
    request: Request,
    svc: Services = Depends(get_services),
    _p: Principal = Depends(require_token),
) -> StreamingResponse:
    async def gen():
        async with svc.events.subscribe() as q:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    type_, data = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {type_}\ndata: {json.dumps(data, default=str)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
