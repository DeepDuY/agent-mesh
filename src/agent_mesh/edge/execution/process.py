from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

logger = logging.getLogger(__name__)


def spawn_kwargs() -> dict:
    """Extra subprocess kwargs: put each task in its own process group (POSIX).

    Grouping the process lets a cancel/timeout kill the whole tree
    (children spawned by `bash -c` or by the LLM runtime's tool calls),
    not just the top-level PID.
    """
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


async def _read_stream(
    stream: asyncio.StreamReader | None,
    chunks: list[bytes],
    limit: int,
    on_line: Any = None,
) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        chunks.append(line)
        if on_line is not None:
            try:
                on_line(line.decode("utf-8", errors="replace"))
            except Exception:
                logger.debug("on_line callback error", exc_info=True)
        total = sum(len(c) for c in chunks)
        while total > limit and len(chunks) > 1:
            total -= len(chunks.pop(0))


def _proc_pgid(proc: asyncio.subprocess.Process) -> int | None:
    """Resolve the task process's group id (created via start_new_session)."""
    if os.name != "posix" or not hasattr(os, "killpg"):
        return None
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return None


def _signal_group(proc: asyncio.subprocess.Process, pgid: int | None, sig: int) -> None:
    """Send a signal to the task's whole process group when possible."""
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        return
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass


async def _terminate_proc(proc: asyncio.subprocess.Process, grace_s: float = 3.0) -> None:
    """Terminate a task's whole process tree.

    Task subprocesses are launched with ``start_new_session=True`` so they form
    their own process group. Signal the whole group (SIGTERM first so LLM
    runtimes / shells can clean up their children, then escalate to SIGKILL)
    instead of killing only the top-level PID, which would leave orphaned
    child processes still running.
    """
    if proc.returncode is not None:
        return
    pgid = _proc_pgid(proc)
    _signal_group(proc, pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_s)
        return
    except asyncio.TimeoutError:
        pass
    _signal_group(proc, pgid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


async def _wait_proc(
    proc: asyncio.subprocess.Process,
    read_task: asyncio.Task,
    timeout_s: float,
    cancel_event: asyncio.Event | None = None,
) -> tuple[int, bool]:
    """Wait for a subprocess to finish, honouring timeout and cancellation.

    Returns ``(exit_code, was_cancelled)``. On timeout or cancellation the
    process is killed and ``-1`` / ``-2`` are returned respectively.
    """
    wait_task = asyncio.create_task(proc.wait())
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event else None
    try:
        if cancel_task:
            done, pending = await asyncio.wait(
                {wait_task, cancel_task},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            done, pending = await asyncio.wait(
                {wait_task}, timeout=timeout_s
            )
        if cancel_task and cancel_task in done:
            await _terminate_proc(proc)
            return -2, True
        if wait_task in done:
            exit_code = wait_task.result()
            await read_task
            return exit_code, False
        # Timeout.
        await _terminate_proc(proc)
        return -1, False
    finally:
        for t in (wait_task, cancel_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except (asyncio.CancelledError, Exception):
                pass


def _tail(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    encoded = text.encode("utf-8")
    tail_bytes = encoded[-limit:]
    while tail_bytes and tail_bytes[0] & 0xC0 == 0x80:
        tail_bytes = tail_bytes[1:]
    return tail_bytes.decode("utf-8", errors="replace")


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""
