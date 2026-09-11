from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from agent_mesh.edge.config_writer import build_opencode_config, find_runtime
from agent_mesh.edge.execution.common import (
    ExecutionOutcome,
    _classify_llm_error,
    _classify_opencode_line,
    _cleanup_config,
    _collect_artifacts,
    _error_outcome,
    _extract_session_id,
    _extract_structured_output,
    _extract_summary_from_json,
    _first_line,
    _read_stream,
    _snapshot_files,
    _tail,
    _wait_proc,
    _wrap_llm_instruction,
    spawn_kwargs,
    stream_task_log,
)
from agent_mesh.shared.schemas import Task

logger = logging.getLogger(__name__)


async def run_llm(
    task: Task,
    workdir: Path,
    runtime: str,
    llm_api_key: str,
    llm_base_url: str,
    llm_model: str,
    cancel_event: asyncio.Event | None = None,
    edge_token: str = "",
    orchestrator_base_url: str = "",
    log_callback=None,
    system_prompt: str = "",
    llm_models: list[str] | None = None,
) -> ExecutionOutcome:
    runtime_path = find_runtime(runtime)
    pre_snapshot = _snapshot_files(workdir)

    # Forced configuration: no hardcoded fallback model. Reject early with a
    # clear message instead of spawning a runtime that cannot resolve a model.
    model = task.constraints.model or llm_model
    if not model:
        return _error_outcome(
            "llm", "no LLM model configured (pass model or set a default model)"
        )

    config = build_opencode_config(
        api_key=llm_api_key,
        base_url=llm_base_url,
        model=model,
        allowed_tools=task.constraints.allowed_tools,
        models=llm_models,
    )

    instruction = task.instruction
    if runtime == "opencode":
        wrapped_instruction = _wrap_llm_instruction(instruction, system_prompt)
        cmd = [
            runtime_path,
            "run",
            "--command",
            "-",
            "--format",
            "json",
            "--auto",
            "--dir",
            str(workdir),
        ]
        if task.constraints.session_id:
            cmd += ["--session", task.constraints.session_id]
    elif runtime == "claude":
        wrapped_instruction = (
            f"{system_prompt.strip()}\n\n{instruction}"
            if system_prompt.strip()
            else instruction
        )
        cmd = [
            runtime_path,
            "-p",
            wrapped_instruction,
        ]
    else:
        return _error_outcome("llm", f"unsupported runtime: {runtime}")

    config_path = workdir / "opencode.json"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    env = os.environ.copy()
    env["OPENCODE_CONFIG"] = str(config_path)
    if edge_token:
        env["EDGE_TOKEN"] = edge_token
    if orchestrator_base_url:
        env["ORCHESTRATOR_URL"] = orchestrator_base_url

    start = datetime.now(timezone.utc)
    logger.info("executing llm task=%s cmd=%s workdir=%s", task.task_id, cmd, workdir)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            env=env,
            **spawn_kwargs(),
        )
        if proc.stdin is not None:
            proc.stdin.write(wrapped_instruction.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
    except FileNotFoundError as e:
        _cleanup_config(config_path)
        return _error_outcome("llm", f"runtime not found: {e}")

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    limit = task.constraints.output_limit

    log_queue: asyncio.Queue | None = None
    log_task: asyncio.Task | None = None
    if log_callback is not None:
        log_queue = asyncio.Queue()

        def _on_llm_line(line: str) -> None:
            entry = _classify_opencode_line(line)
            if entry["content"]:
                log_queue.put_nowait(entry)

        log_task = asyncio.create_task(
            stream_task_log(log_callback, log_queue, cancel_event)
        )
    else:
        _on_llm_line = None

    read_task = asyncio.gather(
        _read_stream(proc.stdout, stdout_chunks, limit, on_line=_on_llm_line),
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
    _cleanup_config(config_path)
    if was_cancelled:
        logger.warning("llm task=%s cancelled by orchestrator", task.task_id)
        return ExecutionOutcome(
            exit_code=-2,
            stdout_tail="",
            stderr_tail="task cancelled",
            duration_ms=int((datetime.now(timezone.utc) - start).total_seconds() * 1000),
            summary="cancelled",
            mode="llm",
            artifacts_paths=[],
        )

    duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    stdout_tail = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_tail = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    structured = _extract_structured_output(stdout_tail)
    if structured:
        summary = structured.get("summary") or structured.get("answer") or ""
        answer = structured.get("answer") or structured.get("summary") or ""
        declared_artifacts = structured.get("artifacts", [])
        artifacts_paths = [a["path"] for a in declared_artifacts if isinstance(a, dict) and a.get("path")]
    else:
        summary = _extract_summary_from_json(stdout_tail)
        answer = stdout_tail
        artifacts_paths = _collect_artifacts(workdir, pre_snapshot)

    if exit_code != 0:
        summary = _classify_llm_error(stdout_tail, stderr_tail)
    elif not summary:
        summary = _first_line(answer) or "llm task completed"

    session_id = _extract_session_id(stdout_tail)

    return ExecutionOutcome(
        exit_code=exit_code,
        stdout_tail=_tail(answer, limit),
        stderr_tail=_tail(stderr_tail, limit),
        duration_ms=duration_ms,
        summary=summary,
        mode="llm",
        artifacts_paths=artifacts_paths,
        session_id=session_id,
    )
