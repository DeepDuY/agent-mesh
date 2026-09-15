from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_LOG_FLUSH_INTERVAL_S = 0.5


@dataclass
class ExecutionOutcome:
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: int
    summary: str
    mode: str = "llm"
    artifacts: list = field(default_factory=list)
    artifacts_paths: list[str] = field(default_factory=list)
    session_id: str | None = None


def _error_outcome(mode: str, summary: str) -> ExecutionOutcome:
    return ExecutionOutcome(
        exit_code=1,
        stdout_tail="",
        stderr_tail=summary,
        duration_ms=0,
        summary=summary,
        mode=mode,
    )


async def stream_task_log(log_callback, queue: asyncio.Queue, cancel_event: asyncio.Event | None) -> None:
    """Periodically flush buffered log entries to the orchestrator via callback."""
    while True:
        entries: list[dict[str, str]] = []
        try:
            first = await asyncio.wait_for(queue.get(), timeout=_LOG_FLUSH_INTERVAL_S)
            entries.append(first)
        except asyncio.TimeoutError:
            pass
        while not queue.empty():
            entries.append(queue.get_nowait())
        if entries and log_callback is not None:
            try:
                await log_callback(entries)
            except Exception:
                logger.warning("task log upload failed", exc_info=True)
        if cancel_event is not None and cancel_event.is_set():
            return
