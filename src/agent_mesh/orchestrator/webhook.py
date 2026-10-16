"""Outbound webhook notifications for task and review events.

A single, global webhook target configured through the ``settings`` KV table:

* ``webhook_enabled``       -- ``"1"`` / ``"0"`` master switch
* ``webhook_kind``          -- ``dingtalk`` | ``feishu`` | ``wecom`` | ``generic``
* ``webhook_url``           -- target URL
* ``webhook_secret``        -- signing secret (DingTalk sign / Feishu sign /
                               generic HMAC header)
* ``webhook_task_events``   -- CSV subset of :data:`TASK_EVENTS`
* ``webhook_review_events`` -- CSV subset of :data:`REVIEW_EVENTS`

Delivery is fire-and-forget (``asyncio.create_task``) and **must never raise
into the task state machine**: every failure is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

TASK_EVENTS: tuple[str, ...] = (
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "denied",
)
REVIEW_EVENTS: tuple[str, ...] = (
    "allowed",
    "denied",
    "pending",
    "approved",
    "rejected",
    "expired",
)
KINDS: tuple[str, ...] = ("generic", "dingtalk", "feishu", "wecom")

DEFAULT_TASK_EVENTS = ",".join(TASK_EVENTS)
DEFAULT_REVIEW_EVENTS = ",".join(REVIEW_EVENTS)

# Settings keys owned by this module (shared with the API/UI layer).
WEBHOOK_SETTING_KEYS: tuple[str, ...] = (
    "webhook_enabled",
    "webhook_kind",
    "webhook_url",
    "webhook_secret",
    "webhook_task_events",
    "webhook_review_events",
)

_TIMEOUT_S = 8.0
_MAX_ATTEMPTS = 3

_TASK_LABELS = {
    "completed": "完成",
    "failed": "失败",
    "timed_out": "超时",
    "cancelled": "已取消",
    "denied": "被拒绝",
}
_REVIEW_LABELS = {
    "allowed": "通过",
    "denied": "拦截",
    "pending": "待人工审核",
    "approved": "人工通过",
    "rejected": "人工拒绝",
    "expired": "超时自动拦截",
}


def _truncate(value: str, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _csv_set(value: str | None) -> set[str]:
    return {part.strip() for part in (value or "").split(",") if part.strip()}


def _event_label(event_key: str) -> str:
    group, _, name = event_key.partition(".")
    if group == "review":
        return f"审核{_REVIEW_LABELS.get(name, name)}"
    return f"任务{_TASK_LABELS.get(name, name)}"


async def _load_config(store: Any, *, require_enabled: bool = True) -> dict[str, Any] | None:
    """Read webhook settings. Returns ``None`` when disabled/unconfigured."""
    settings = await store.list_settings()
    if require_enabled and (settings.get("webhook_enabled") or "0") != "1":
        return None
    url = (settings.get("webhook_url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return None
    kind = (settings.get("webhook_kind") or "generic").strip().lower()
    if kind not in KINDS:
        kind = "generic"
    # A present (possibly empty) CSV is honoured literally, so unchecking every
    # event means "push nothing"; a missing key falls back to all events.
    task_raw = settings.get("webhook_task_events")
    review_raw = settings.get("webhook_review_events")
    return {
        "kind": kind,
        "url": url,
        "secret": (settings.get("webhook_secret") or "").strip(),
        "task_events": (
            _csv_set(task_raw) if task_raw is not None else set(TASK_EVENTS)
        ),
        "review_events": (
            _csv_set(review_raw) if review_raw is not None else set(REVIEW_EVENTS)
        ),
    }


def _task_info(task: Any) -> dict[str, Any]:
    result = getattr(task, "result", None)
    info: dict[str, Any] = {
        "task_id": task.task_id,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "agent_id": task.agent_id,
        "mode": task.mode,
    }
    if task.dispatched_by:
        info["dispatched_by"] = task.dispatched_by
    if task.instruction:
        info["instruction"] = _truncate(task.instruction, 200)
    if result is not None:
        info["summary"] = result.summary
        info["exit_code"] = result.exit_code
        info["duration_ms"] = result.duration_ms
    info["time"] = _fmt_dt(task.finished_at or task.created_at)
    return info


def _review_info(task: Any, detail: dict[str, Any] | None) -> dict[str, Any] | None:
    detail = detail or {}
    info: dict[str, Any] = {}
    score = detail.get("score", getattr(task, "review_score", None))
    reason = detail.get("reason", getattr(task, "review_reason", None))
    if score is not None:
        info["score"] = score
    if detail.get("threshold") is not None:
        info["threshold"] = detail["threshold"]
    if detail.get("action"):
        info["action"] = detail["action"]
    if reason:
        info["reason"] = reason
    if detail.get("violations"):
        info["violations"] = detail["violations"]
    if detail.get("model"):
        info["model"] = detail["model"]
    if detail.get("source"):
        info["source"] = detail["source"]
    if detail.get("actor"):
        info["actor"] = detail["actor"]
    return info or None


def build_task_event(task: Any, event: str) -> tuple[str, str, dict, dict | None]:
    review = _review_info(task, None)
    return f"task.{event}", _event_label(f"task.{event}"), _task_info(task), review


def build_review_event(
    task: Any, action: str, detail: dict[str, Any] | None = None
) -> tuple[str, str, dict, dict | None]:
    review = _review_info(task, detail)
    return f"review.{action}", _event_label(f"review.{action}"), _task_info(task), review


def _render_lines(
    label: str,
    task_info: dict[str, Any] | None,
    review_info: dict[str, Any] | None,
) -> list[str]:
    lines = [f"【{label}】"]
    if task_info:
        lines.append(f"任务ID: {task_info.get('task_id', '-')}")
        lines.append(f"状态: {task_info.get('status', '-')}")
        lines.append(f"节点: {task_info.get('agent_id', '-')}")
        lines.append(f"模式: {task_info.get('mode', '-')}")
        if task_info.get("dispatched_by"):
            lines.append(f"派发人: {task_info['dispatched_by']}")
        if task_info.get("instruction"):
            lines.append(f"指令: {task_info['instruction']}")
        if task_info.get("summary") is not None:
            lines.append(f"结果: {task_info['summary']}")
        if task_info.get("exit_code") is not None:
            lines.append(f"退出码: {task_info['exit_code']}")
        if task_info.get("duration_ms") is not None:
            lines.append(f"耗时: {task_info['duration_ms']} ms")
        if task_info.get("time"):
            lines.append(f"时间: {task_info['time']}")
    if review_info:
        lines.append("— 审核信息 —")
        if review_info.get("score") is not None:
            threshold = review_info.get("threshold")
            suffix = f"（阈值 {threshold}）" if threshold is not None else ""
            lines.append(f"评分: {review_info['score']}{suffix}")
        if review_info.get("action"):
            lines.append(f"结论: {review_info['action']}")
        if review_info.get("reason"):
            lines.append(f"原因: {review_info['reason']}")
        violations = review_info.get("violations")
        if violations:
            lines.append("违规点: " + "; ".join(str(v) for v in violations))
        if review_info.get("model"):
            lines.append(f"审核模型: {review_info['model']}")
        if review_info.get("source"):
            lines.append(f"来源: {review_info['source']}")
        if review_info.get("actor"):
            lines.append(f"处理人: {review_info['actor']}")
    return lines


def _sign_dingtalk(secret: str) -> dict[str, str]:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return {"timestamp": timestamp, "sign": base64.b64encode(digest).decode()}


def _sign_feishu(secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode(), b"", hashlib.sha256).digest()
    return {"timestamp": timestamp, "sign": base64.b64encode(digest).decode()}


def _build_request(
    cfg: dict[str, Any],
    label: str,
    event_key: str,
    task_info: dict[str, Any] | None,
    review_info: dict[str, Any] | None,
) -> tuple[str, dict[str, str], str]:
    """Return ``(url, headers, raw_body)`` for the configured platform."""
    kind = cfg["kind"]
    url = cfg["url"]
    lines = _render_lines(label, task_info, review_info)
    text = "\n".join(lines)
    headers = {"Content-Type": "application/json"}

    if kind == "dingtalk":
        body = {"msgtype": "markdown", "markdown": {"title": label, "text": text}}
        if cfg["secret"]:
            params = _sign_dingtalk(cfg["secret"])
            joiner = "&" if "?" in url else "?"
            url = url + joiner + urlencode(params)
    elif kind == "wecom":
        body = {"msgtype": "markdown", "markdown": {"content": text}}
    elif kind == "feishu":
        body = {"msg_type": "text", "content": {"text": text}}
        if cfg["secret"]:
            body.update(_sign_feishu(cfg["secret"]))
    else:  # generic
        body = {"event": event_key, "title": label, "text": text}
        if task_info:
            body["task"] = task_info
        if review_info:
            body["review"] = review_info
        raw = json.dumps(body, ensure_ascii=False)
        if cfg["secret"]:
            digest = hmac.new(
                cfg["secret"].encode(), raw.encode(), hashlib.sha256
            ).hexdigest()
            headers["X-Agent-Mesh-Signature"] = f"sha256={digest}"
        return url, headers, raw

    return url, headers, json.dumps(body, ensure_ascii=False)


def _check_platform_error(kind: str, resp: httpx.Response) -> str | None:
    """DingTalk/WeCom/Feishu return HTTP 200 with an ``errcode``/``code``."""
    if kind == "generic":
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("errcode", data.get("code"))
    if code in (None, 0):
        return None
    return str(data.get("errmsg") or data.get("msg") or code)


async def _deliver(
    cfg: dict[str, Any],
    label: str,
    event_key: str,
    task_info: dict[str, Any] | None,
    review_info: dict[str, Any] | None,
) -> dict[str, Any]:
    url, headers, raw = _build_request(cfg, label, event_key, task_info, review_info)
    last_error = "unknown error"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.post(url, content=raw, headers=headers)
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
            else:
                platform_error = _check_platform_error(cfg["kind"], resp)
                if platform_error is None:
                    return {"ok": True, "status": resp.status_code, "attempt": attempt}
                last_error = f"platform error: {platform_error}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        except Exception as exc:  # never let a webhook bug propagate
            logger.warning("webhook delivery error (%s)", exc)
            return {"ok": False, "detail": str(exc)}
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
    logger.warning(
        "webhook delivery failed event=%s url=%s: %s", event_key, cfg["url"], last_error
    )
    return {"ok": False, "detail": last_error}


async def notify_task_event(task: Any, event: str, store: Any) -> None:
    """Push a task terminal event. Swallows every error."""
    try:
        cfg = await _load_config(store)
        if cfg is None or event not in cfg["task_events"]:
            return
        event_key, label, task_info, review_info = build_task_event(task, event)
        await _deliver(cfg, label, event_key, task_info, review_info)
    except Exception:
        logger.exception("webhook task-event notification failed")


async def notify_review_event(
    task: Any, action: str, detail: dict[str, Any] | None, store: Any
) -> None:
    """Push an AI-review event. Swallows every error."""
    try:
        cfg = await _load_config(store)
        if cfg is None or action not in cfg["review_events"]:
            return
        event_key, label, task_info, review_info = build_review_event(task, action, detail)
        await _deliver(cfg, label, event_key, task_info, review_info)
    except Exception:
        logger.exception("webhook review-event notification failed")


async def send_test(store: Any) -> dict[str, Any]:
    """Send a sample notification using the current settings (for the UI)."""
    cfg = await _load_config(store, require_enabled=False)
    if cfg is None:
        return {"ok": False, "detail": "未配置 webhook 地址"}
    task_info = {
        "task_id": "t-test0000",
        "status": "completed",
        "agent_id": "test-node",
        "mode": "llm",
        "dispatched_by": "test",
        "instruction": "这是一条 webhook 测试通知",
        "summary": "测试成功",
        "exit_code": 0,
        "duration_ms": 1234,
        "time": _fmt_dt(datetime.now(timezone.utc)),
    }
    return await _deliver(cfg, "任务完成（测试）", "task.completed", task_info, None)
