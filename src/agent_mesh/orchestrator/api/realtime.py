"""Server-Sent Events endpoint for live dashboard updates.

The Web UI opens this with ``fetch`` (not ``EventSource``) so it can send the
normal ``Authorization`` header — putting a token in the query string would
leak it into access logs. Clients that cannot hold the stream keep using the
existing interval polling as a fallback.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from agent_mesh.orchestrator.realtime import hub

# Comment frames keep intermediaries from timing the idle connection out.
_PING_SECONDS = 15.0


def mount_realtime_routes(router: APIRouter, require_any_token) -> None:
    @router.get("/realtime")
    async def realtime(
        request: Request,
        _auth: dict = Depends(require_any_token),
    ) -> StreamingResponse:
        async def stream():
            queue = hub.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=_PING_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
