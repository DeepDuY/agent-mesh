from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_FLUSH_INTERVAL_S = 0.5


def spawn_kwargs() -> dict:
    """Extra subprocess kwargs: put each task in its own process group (POSIX).

    Grouping the process lets a cancel/timeout kill the whole tree
    (children spawned by `bash -c` or by the LLM runtime's tool calls),
    not just the top-level PID.
    """
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


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


def _cleanup_config(config_path: Path) -> None:
    try:
        if config_path and config_path.exists():
            config_path.unlink()
    except Exception:
        pass


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


def _snapshot_files(workdir: Path) -> set[str]:
    snapshot: set[str] = set()
    if not workdir.exists():
        return snapshot
    for p in workdir.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(workdir).parts):
            snapshot.add(str(p.relative_to(workdir)))
    return snapshot


def _collect_artifacts(workdir: Path, pre_snapshot: set[str]) -> list[str]:
    artifacts: list[str] = []
    if not workdir.exists():
        return artifacts
    for p in workdir.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(workdir).parts):
            rel = str(p.relative_to(workdir))
            if rel not in pre_snapshot:
                artifacts.append(rel)
    return artifacts


def _extract_session_id(stdout: str) -> str | None:
    """Pull the opencode `sessionID` from the JSONL event stream (last wins)."""
    session_id: str | None = None
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("sessionID"), str):
            session_id = obj["sessionID"]
    return session_id


def _classify_opencode_line(line: str) -> dict[str, str]:
    """Classify one opencode `--format json` JSONL line into a log entry.

    Returns ``{"kind": "text"|"error"|"complete"|"raw", "content": "..."}`` so the
    live task log can show what the LLM is actually producing (not just raw JSON).
    """
    line = line.strip()
    if not line:
        return {"kind": "raw", "content": ""}
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {"kind": "raw", "content": line}
    if not isinstance(obj, dict):
        return {"kind": "raw", "content": line}

    typ = obj.get("type")
    if typ == "text":
        part = obj.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return {"kind": "text", "content": part["text"]}
        return {"kind": "raw", "content": line}
    if typ == "error":
        err = obj.get("error")
        if isinstance(err, str):
            return {"kind": "error", "content": err}
        if isinstance(err, dict):
            msg = err.get("message") or err.get("text") or str(err)
            return {"kind": "error", "content": str(msg)}
        return {"kind": "error", "content": line}
    if typ == "complete":
        summary = obj.get("summary") or obj.get("message") or obj.get("text") or ""
        return {"kind": "complete", "content": str(summary)}
    # Tool events / progress: surface a short human hint, else fall back to raw.
    if typ == "tool":
        tool = obj.get("tool")
        state = obj.get("state") or obj.get("status") or ""
        name = tool.get("name") if isinstance(tool, dict) else str(tool or typ)
        return {"kind": "raw", "content": f"[tool:{name} {state}]".strip()}
    if typ in ("message", "session", "step", "reasoning"):
        content = obj.get("text") or obj.get("message") or obj.get("label") or ""
        if content:
            return {"kind": "text", "content": str(content)}
    return {"kind": "raw", "content": line}


def _wrap_llm_instruction(instruction: str, system_prompt: str = "") -> str:
    role_block = ""
    if system_prompt.strip():
        role_block = (
            "## 角色与上下文\n"
            f"{system_prompt.strip()}\n\n"
        )
    return (
        role_block
        + "You are an agent task executor. Follow the user's instruction, use tools as needed, "
        "and produce the final answer in the exact JSON format below. Do not output anything "
        "after the JSON block.\n\n"
        "```json\n"
        "{\n"
        '  "summary": "one-line summary of the result",\n'
        '  "answer": "the full answer to return to the caller",\n'
        '  "artifacts": [\n'
        '    {"path": "relative/path/under/workdir", "compressed": true}\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "Rules for artifacts:\n"
        "- Only list files you actually created in the workdir.\n"
        "- Paths must be relative to the workdir.\n"
        "- Large files (>1 MB) or multiple related files must be compressed into a .tar.gz or .zip.\n"
        "- If no artifacts, use an empty list [].\n\n"
        f"User instruction: {instruction}\n\n"
        "## 任务附件\n"
        "工作目录下可能已放入任务附件文件（派发时附加）。执行前先用 ls 查看工作目录，"
        "若存在附件文件则按需读取使用；若指令与附件无关可直接忽略本段。\n\n"
        "## 技能库（按需使用）\n"
        "本环境提供技能库。若当前任务需要专业技能，可自行通过以下方式查看并按需下载使用；"
        "没有合适技能时直接忽略本段，无需特意访问：\n"
        "- 查看可用技能摘要：`curl -s -H \"Authorization: Bearer ${EDGE_TOKEN:-}\" \"${ORCHESTRATOR_URL:-}/api/skills\"`\n"
        "- 若从摘要中发现合适的技能，下载其压缩包并解压使用（把 <skill-name> 换成实际技能名）：\n"
        "  `curl -s -H \"Authorization: Bearer ${EDGE_TOKEN:-}\" -o /tmp/skill.zip \"${ORCHESTRATOR_URL:-}/api/skills/<skill-name>/download\" "
        "&& mkdir -p .opencode/skills/<skill-name> && unzip -o /tmp/skill.zip -d .opencode/skills/<skill-name> "
        "&& cat .opencode/skills/<skill-name>/SKILL.md`\n"
        "- 阅读 SKILL.md 后按其说明执行；下载的文件若只是参考说明，不必列为产物。\n\n"
        "Now produce the final JSON:"
    )


def _extract_structured_output(stdout: str) -> dict[str, Any] | None:
    text = stdout

    text_parts: list[str] = []
    last_error: str | None = None
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        typ = obj.get("type")
        part = obj.get("part")
        if typ == "text" and isinstance(part, dict) and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        elif typ == "error":
            err = obj.get("error")
            last_error = str(err) if not isinstance(err, str) else err

    if text_parts:
        joined = "\n".join(text_parts)
        parsed = _parse_json_block(joined)
        if parsed:
            return parsed
        parsed = _find_structured_json(joined)
        if parsed:
            return parsed
        if last_error:
            return {"summary": last_error, "answer": last_error, "artifacts": []}

    return _find_structured_json(text)


def _find_structured_json(text: str) -> dict[str, Any] | None:
    for candidate in reversed(_extract_json_objects(text)):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("summary" in obj or "answer" in obj):
            return obj
    return None


def _extract_json_objects(text: str) -> list[str]:
    results: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escaped = False
        j = i
        while j < n:
            c = text[j]
            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        results.append(text[i : j + 1])
                        i = j + 1
                        break
            j += 1
        else:
            i += 1
    return results


def _parse_json_block(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        candidate = text.strip()
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict):
        return obj
    return None


def _extract_summary_from_json(stdout: str) -> str:
    lines = stdout.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if obj.get("type") == "error":
                err = obj.get("error", {})
                return str(err) if not isinstance(err, str) else err
            if obj.get("type") == "complete":
                summary = obj.get("summary") or obj.get("message") or ""
                return str(summary) if not isinstance(summary, str) else summary
            if "summary" in obj:
                summary = obj["summary"]
                return str(summary) if not isinstance(summary, str) else summary
            if "content" in obj:
                content = obj["content"]
                return str(content) if not isinstance(content, str) else content
    return ""


def _classify_llm_error(stdout: str, stderr: str) -> str:
    combined = (stdout + " " + stderr).lower()
    if "fetch" in combined and "url" in combined:
        return "LLM 配置错误：无法连接模型服务，请检查 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL"
    if "authentication" in combined or "unauthorized" in combined or "401" in combined:
        return "LLM 认证失败：请检查 LLM_API_KEY 是否正确"
    if "timeout" in combined:
        return "LLM 请求超时"
    if "unexpected server error" in combined or "unknownerror" in combined:
        return "LLM 服务错误：无法连接模型服务，请检查 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL"
    if stderr.strip():
        return f"LLM 执行失败：{_first_line(stderr)}"
    return "LLM 执行失败"
