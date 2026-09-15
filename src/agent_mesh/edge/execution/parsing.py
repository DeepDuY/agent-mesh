from __future__ import annotations

import json
import re
from typing import Any

from agent_mesh.edge.execution.process import _first_line


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
