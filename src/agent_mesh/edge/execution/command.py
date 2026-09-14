from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from agent_mesh.edge.execution.common import (
    ExecutionOutcome,
    _collect_artifacts,
    _error_outcome,
    _first_line,
    _read_stream,
    _snapshot_files,
    _tail,
    _wait_proc,
    spawn_kwargs,
    stream_task_log,
)
from agent_mesh.shared.schemas import Task

logger = logging.getLogger(__name__)


async def run_command(
    task: Task,
    workdir: Path,
    cancel_event: asyncio.Event | None = None,
    log_callback=None,
    permission: dict | None = None,
) -> ExecutionOutcome:
    # Command mode does not go through OpenCode, so the shared permission policy
    # is evaluated here before the shell is spawned. Any sub-command that is not
    # explicitly allowed (deny/ask) rejects the whole task. This is a guard
    # against destructive/accidental commands, not a security sandbox.
    from agent_mesh.shared import permissions

    if permissions.evaluate_command(permission, task.instruction) != "allow":
        logger.warning(
            "command task=%s denied by permission policy: %s",
            task.task_id,
            task.instruction,
        )
        if log_callback is not None:
            try:
                await log_callback(
                    [{"kind": "error", "content": f"权限被拒绝: {task.instruction}"}]
                )
            except Exception:
                logger.debug("denied-log upload failed", exc_info=True)
        return _error_outcome("command", f"权限被拒绝: {task.instruction}")

    pre_snapshot = _snapshot_files(workdir)
    start = datetime.now(timezone.utc)

    cmd = ["bash", "-c", task.instruction]
    logger.info("executing command task=%s cmd=%s workdir=%s", task.task_id, cmd, workdir)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            **spawn_kwargs(),
        )
    except FileNotFoundError as e:
        return _error_outcome("command", f"bash not found: {e}")

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    limit = task.constraints.output_limit

    log_queue: asyncio.Queue | None = None
    log_task: asyncio.Task | None = None
    if log_callback is not None:
        log_queue = asyncio.Queue()
        log_task = asyncio.create_task(
            stream_task_log(log_callback, log_queue, cancel_event)
        )

        def _on_line(_line: str) -> None:
            # Command-mode output is surfaced verbatim as raw live log lines.
            log_queue.put_nowait({"kind": "raw", "content": _line.rstrip("\n")})

    else:
        _on_line = None

    read_task = asyncio.gather(
        _read_stream(proc.stdout, stdout_chunks, limit, on_line=_on_line),
        _read_stream(proc.stderr, stderr_chunks, limit),
    )
    try:
        exit_code, was_cancelled = await _wait_proc(
            proc, read_task, task.constraints.timeout_s, cancel_event
        )
    finally:
        if log_task is not None:
            log_task.cancel()
            try:
                await log_task
            except (asyncio.CancelledError, Exception):
                pass
    if was_cancelled:
        logger.warning("command task=%s cancelled by orchestrator", task.task_id)
        return ExecutionOutcome(
            exit_code=-2,
            stdout_tail="",
            stderr_tail="task cancelled",
            duration_ms=int((datetime.now(timezone.utc) - start).total_seconds() * 1000),
            summary="cancelled",
            mode="command",
            artifacts_paths=[],
        )

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    stdout_tail = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_tail = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    if exit_code == 0:
        summary = _first_line(stdout_tail) or "command completed"
    else:
        summary = f"command failed (exit code {exit_code})"
        if stderr_tail:
            summary += ": " + _first_line(stderr_tail)

    artifacts_paths = _collect_artifacts(workdir, pre_snapshot)

    return ExecutionOutcome(
        exit_code=exit_code,
        stdout_tail=_tail(stdout_tail, limit),
        stderr_tail=_tail(stderr_tail, limit),
        duration_ms=duration_ms,
        summary=summary,
        mode="command",
        artifacts_paths=artifacts_paths,
    )
