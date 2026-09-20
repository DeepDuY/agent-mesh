"""In-process pub/sub used by the SSE endpoint (``/api/realtime``).

The orchestrator currently runs a single process, so an in-memory hub is
enough. If uvicorn is ever moved to multiple workers, this must be backed by a
shared bus (e.g. Postgres ``LISTEN/NOTIFY``) so events reach clients connected
to other workers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class RealtimeHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow/stuck client must never block the publisher.
                logger.debug("realtime subscriber queue full; dropping event")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


hub = RealtimeHub()


def publish(event_type: str, **data: Any) -> None:
    """Broadcast an event to connected dashboards (no-op when nobody listens)."""
    if not hub.subscriber_count:
        return
    hub.publish({"type": event_type, **data})
