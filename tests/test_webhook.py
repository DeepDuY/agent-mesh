"""Outbound webhook notifications: formatting, signing, filtering, delivery."""

from __future__ import annotations

import json

import pytest

from agent_mesh.orchestrator import webhook
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import Constraints, Task, TaskResult


class DummyStore:
    def __init__(self, settings: dict[str, str]):
        self._settings = settings

    async def list_settings(self) -> dict[str, str]:
        return dict(self._settings)


def _task(status: TaskStatus = TaskStatus.COMPLETED, **kw) -> Task:
    data = dict(
        task_id="t-abc12345",
        agent_id="node-1",
        mode="llm",
        instruction="run the tests",
        constraints=Constraints(),
        status=status,
        dispatched_by="alice",
        result=TaskResult(
            status="completed",
            exit_code=0,
            stdout_tail="ok",
            stderr_tail="",
            duration_ms=42,
            summary="all good",
        ),
        review_score=88,
        review_reason="看起来没问题",
    )
    data.update(kw)
    return Task(**data)


def _settings(**kw) -> DummyStore:
    base = {
        "webhook_enabled": "1",
        "webhook_kind": "generic",
        "webhook_url": "https://example.com/hook",
        "webhook_secret": "",
        "webhook_task_events": "completed,failed,timed_out,cancelled,denied",
        "webhook_review_events": "allowed,denied,pending,approved,rejected,expired",
    }
    base.update(kw)
    return DummyStore(base)


# ---------------------------------------------------------------------------
# Config loading / filtering
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_load_config_disabled_returns_none():
    assert await webhook._load_config(_settings(webhook_enabled="0")) is None


@pytest.mark.asyncio
async def test_load_config_empty_url_returns_none():
    assert await webhook._load_config(_settings(webhook_url="")) is None


@pytest.mark.asyncio
async def test_load_config_rejects_non_http_url():
    assert await webhook._load_config(_settings(webhook_url="ftp://x/y")) is None


@pytest.mark.asyncio
async def test_load_config_empty_events_means_none():
    cfg = await webhook._load_config(
        _settings(webhook_task_events="", webhook_review_events="")
    )
    assert cfg is not None
    assert cfg["task_events"] == set()
    assert cfg["review_events"] == set()


# ---------------------------------------------------------------------------
# Payload formatting per platform
# ---------------------------------------------------------------------------
def _cfg(kind: str, secret: str = "") -> dict:
    return {
        "kind": kind,
        "url": "https://example.com/hook",
        "secret": secret,
        "task_events": set(webhook.TASK_EVENTS),
        "review_events": set(webhook.REVIEW_EVENTS),
    }


def test_generic_payload_has_task_and_review():
    event_key, label, task_info, review_info = webhook.build_task_event(
        _task(), "completed"
    )
    url, headers, raw = webhook._build_request(
        _cfg("generic"), label, event_key, task_info, review_info
    )
    body = json.loads(raw)
    assert body["event"] == "task.completed"
    assert body["task"]["task_id"] == "t-abc12345"
    assert body["review"]["score"] == 88
    assert headers["Content-Type"] == "application/json"
    assert "X-Agent-Mesh-Signature" not in headers


def test_generic_hmac_signature_header():
    _, label, task_info, review_info = webhook.build_task_event(_task(), "completed")
    _, headers, raw = webhook._build_request(
        _cfg("generic", "s3cr3t"), label, "task.completed", task_info, review_info
    )
    assert headers["X-Agent-Mesh-Signature"].startswith("sha256=")


def test_dingtalk_payload_and_sign():
    _, label, task_info, review_info = webhook.build_task_event(_task(), "completed")
    url, _, raw = webhook._build_request(
        _cfg("dingtalk", "s3cr3t"), label, "task.completed", task_info, review_info
    )
    body = json.loads(raw)
    assert body["msgtype"] == "markdown"
    assert "task.completed" not in body["markdown"]["text"]
    assert "t-abc12345" in body["markdown"]["text"]
    assert "timestamp=" in url and "sign=" in url


def test_feishu_payload_plain_text():
    _, label, task_info, review_info = webhook.build_task_event(_task(), "failed")
    _, _, raw = webhook._build_request(
        _cfg("feishu"), label, "task.failed", task_info, review_info
    )
    body = json.loads(raw)
    assert body["msg_type"] == "text"
    assert "t-abc12345" in body["content"]["text"]


def test_wecom_payload_markdown():
    _, label, task_info, review_info = webhook.build_task_event(_task(), "completed")
    _, _, raw = webhook._build_request(
        _cfg("wecom"), label, "task.completed", task_info, review_info
    )
    body = json.loads(raw)
    assert body["msgtype"] == "markdown"
    assert "t-abc12345" in body["markdown"]["content"]


def test_review_event_payload_includes_review_detail():
    _, label, task_info, review_info = webhook.build_review_event(
        _task(status=TaskStatus.DENIED),
        "denied",
        {
            "action": "denied",
            "score": 12,
            "threshold": 60,
            "reason": "危险命令",
            "violations": ["rm -rf"],
            "model": "reviewer",
            "source": "dispatch",
            "actor": "ai",
        },
    )
    body = json.loads(
        webhook._build_request(_cfg("generic"), label, "review.denied", task_info, review_info)[2]
    )
    assert body["event"] == "review.denied"
    assert body["review"]["score"] == 12
    assert body["review"]["violations"] == ["rm -rf"]


# ---------------------------------------------------------------------------
# Delivery / filtering behaviour
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_notify_filters_unsubscribed_event(monkeypatch):
    calls = []

    async def fake_deliver(*args, **kwargs):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(webhook, "_deliver", fake_deliver)
    store = _settings(webhook_task_events="completed")
    await webhook.notify_task_event(_task(), "failed", store)
    assert calls == []
    await webhook.notify_task_event(_task(), "completed", store)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_notify_swallows_delivery_errors(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(webhook, "_deliver", boom)
    # must not raise
    await webhook.notify_task_event(_task(), "completed", _settings())
    await webhook.notify_review_event(_task(), "allowed", {}, _settings())


@pytest.mark.asyncio
async def test_deliver_retries_then_succeeds(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            self.count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            FakeClient.last_count += 1
            return FakeResponse(500 if FakeClient.last_count < 3 else 200)

    FakeClient.last_count = 0
    monkeypatch.setattr(webhook.httpx, "AsyncClient", FakeClient)

    result = await webhook._deliver(_cfg("generic"), "任务完成", "task.completed", {}, None)
    assert result["ok"] is True
    assert FakeClient.last_count == 3


# ---------------------------------------------------------------------------
# Integration: a failing webhook never breaks the task state machine
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_submit_result_survives_webhook_failure(monkeypatch):
    from agent_mesh.orchestrator.store.sqlite import SQLiteStore
    from agent_mesh.orchestrator.task_store import TaskStore
    import tempfile
    from pathlib import Path

    async def boom(*args, **kwargs):
        raise RuntimeError("webhook exploded")

    monkeypatch.setattr(webhook, "_deliver", boom)

    tmp = tempfile.mkdtemp()
    backend = SQLiteStore(str(Path(tmp) / "am.db"))
    await backend.initialize()
    store = TaskStore(store=backend)
    await backend.set_setting("webhook_enabled", "1")
    await backend.set_setting("webhook_url", "https://example.com/hook")
    try:
        await store.heartbeat("client", "00:aa:bb:cc:dd:09", "opencode", "host")
        task_id = await store.dispatch(
            agent_id="00:aa:bb:cc:dd:09", instruction="hello"
        )
        tasks, _ = await store.heartbeat(
            "client", "00:aa:bb:cc:dd:09", "opencode", "host"
        )
        await store.mark_started(tasks[0].task_id)
        ok = await store.submit_result(
            tasks[0].task_id,
            TaskResult(
                status="completed",
                exit_code=0,
                stdout_tail="ok",
                stderr_tail="",
                duration_ms=1,
                summary="done",
            ),
        )
        assert ok is True
        assert (await store.get_task(task_id)).status == TaskStatus.COMPLETED
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------
def _admin() -> dict:
    return {"Authorization": "Bearer admin-api-token-123", "X-Agent-Mesh-UI": "1"}


def test_settings_expose_webhook_keys(client):
    r = client.get("/api/settings", headers=_admin())
    assert r.status_code == 200, r.text
    settings = r.json()["settings"]
    assert settings["webhook_enabled"] == "0"
    assert settings["webhook_kind"] == "generic"
    assert settings["webhook_task_events"] == "completed,failed,timed_out,cancelled,denied"


def test_test_webhook_endpoint_reports_unconfigured(client):
    r = client.post("/api/settings:test_webhook", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
