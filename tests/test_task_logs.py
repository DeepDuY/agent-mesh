from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _edge_headers() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _make_task(client) -> str:
    client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01",
              "runtime": "opencode", "hostname": "h", "os": "linux"},
        headers=_edge_headers(),
    )
    return client.post(
        "/api/tasks/dispatch",
        json={"agent_id": "00:aa:bb:cc:dd:01", "mode": "command", "instruction": "hello"},
        headers=_admin_headers(),
    ).json()["task_id"]


def test_edge_uploads_logs_and_user_reads_incrementally(client: TestClient):
    task_id = _make_task(client)

    r = client.post(
        "/api/edge/task_log",
        json={"task_id": task_id, "entries": [
            {"kind": "text", "content": "思考中..."},
            {"kind": "text", "content": "分析文件"},
        ]},
        headers=_edge_headers(),
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert r.json()["count"] == 2

    # Full read from id 0.
    logs = client.get(f"/api/tasks/{task_id}/logs?after_id=0", headers=_admin_headers()).json()
    assert len(logs["logs"]) == 2
    assert logs["logs"][0]["content"] == "思考中..."
    assert logs["next_id"] == 2

    # Incremental read: only newer entries.
    client.post(
        "/api/edge/task_log",
        json={"task_id": task_id, "entries": [{"kind": "error", "content": "boom"}]},
        headers=_edge_headers(),
    )
    inc = client.get(f"/api/tasks/{task_id}/logs?after_id=2", headers=_admin_headers()).json()
    assert len(inc["logs"]) == 1
    assert inc["logs"][0]["kind"] == "error"
    assert inc["logs"][0]["content"] == "boom"
    assert inc["next_id"] == 3


def test_task_log_rejects_invalid_kind_and_empty(client: TestClient):
    task_id = _make_task(client)
    r = client.post(
        "/api/edge/task_log",
        json={"task_id": task_id, "entries": [{"kind": "bogus", "content": "x"}]},
        headers=_edge_headers(),
    )
    assert r.json()["accepted"] is True
    assert r.json()["count"] == 1  # invalid kind normalized to "raw"
    logs = client.get(f"/api/tasks/{task_id}/logs", headers=_admin_headers()).json()
    assert logs["logs"][0]["kind"] == "raw"

    r = client.post(
        "/api/edge/task_log",
        json={"task_id": task_id, "entries": []},
        headers=_edge_headers(),
    )
    assert r.json()["accepted"] is False


def test_task_log_requires_token(client: TestClient):
    task_id = _make_task(client)
    resp = client.post(
        "/api/edge/task_log",
        json={"task_id": task_id, "entries": [{"kind": "text", "content": "x"}]},
    )
    assert resp.status_code == 401
    resp = client.get(f"/api/tasks/{task_id}/logs")
    assert resp.status_code == 401


def test_edge_ingest_strips_nul_bytes(client: TestClient):
    task_id = _make_task(client)
    # Claim the task so submit_result is accepted.
    client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01",
              "runtime": "opencode", "hostname": "h", "os": "linux"},
        headers=_edge_headers(),
    )

    # NUL bytes (common in Windows UTF-16 output) must not break ingest: the
    # PostgreSQL text columns reject 0x00, which used to 500 and drop results.
    r = client.post(
        "/api/edge/task_log",
        json={"task_id": task_id, "entries": [{"kind": "raw", "content": "a\x00b"}]},
        headers=_edge_headers(),
    )
    assert r.status_code == 200 and r.json()["accepted"] is True
    logs = client.get(f"/api/tasks/{task_id}/logs", headers=_admin_headers()).json()
    assert logs["logs"][0]["content"] == "ab"

    r = client.post(
        "/api/edge/submit_result",
        json={
            "task_id": task_id, "status": "completed", "mode": "command",
            "exit_code": 0, "stdout_tail": "x\x00y", "stderr_tail": "",
            "summary": "s\x00", "duration_ms": 1,
        },
        headers=_edge_headers(),
    )
    assert r.status_code == 200 and r.json()["accepted"] is True
    task = client.get(f"/api/tasks/{task_id}", headers=_admin_headers()).json()["task"]
    assert task["result"]["stdout_tail"] == "xy"
    assert task["result"]["summary"] == "s"


def test_delete_task_cascades_logs(client: TestClient):
    task_id = _make_task(client)
    client.post(
        "/api/edge/task_log",
        json={"task_id": task_id, "entries": [{"kind": "text", "content": "x"}]},
        headers=_edge_headers(),
    )
    assert client.delete(f"/api/tasks/{task_id}", headers=_admin_headers()).status_code == 200
    # The task (and its logs) are gone: the logs endpoint now 404s.
    assert client.get(f"/api/tasks/{task_id}/logs", headers=_admin_headers()).status_code == 404


def test_task_logs_store_roundtrip(tmp_path):
    import asyncio

    from agent_mesh.orchestrator.store.sqlite import SQLiteStore

    async def _run():
        backend = SQLiteStore(str(tmp_path / "agent-mesh.db"))
        await backend.initialize()
        try:
            await backend.append_task_logs("t-1", [
                {"kind": "text", "content": "a"},
                {"kind": "error", "content": "b"},
            ])
            logs = await backend.list_task_logs("t-1")
            assert [l["content"] for l in logs] == ["a", "b"]
            assert [l["kind"] for l in logs] == ["text", "error"]
            inc = await backend.list_task_logs("t-1", after_id=logs[0]["id"])
            assert len(inc) == 1 and inc[0]["content"] == "b"
            await backend.delete_task_logs("t-1")
            assert await backend.list_task_logs("t-1") == []
        finally:
            await backend.close()

    asyncio.run(_run())
