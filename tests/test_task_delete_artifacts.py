"""Deleting a task must also remove its artifacts from disk (not just the DB)."""

from __future__ import annotations

import pytest_asyncio
from fastapi.testclient import TestClient

from conftest import sqlite_client

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _edge() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


@pytest_asyncio.fixture
async def client(tmp_path):
    async with sqlite_client(tmp_path, artifact_dir=str(tmp_path / "artifacts")) as c:
        yield c


def _make_task(client: TestClient) -> str:
    client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "client",
            "device_id": "00:aa:bb:cc:dd:01",
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
        },
        headers=_edge(),
    )
    return client.post(
        "/api/tasks/dispatch",
        json={"agent_id": "00:aa:bb:cc:dd:01", "mode": "command", "instruction": "hello"},
        headers=_admin(),
    ).json()["task_id"]


def _seed_artifact(tmp_path, task_id: str):
    d = tmp_path / "artifacts" / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "a-1_report.txt").write_bytes(b"data")
    return d


def test_delete_task_removes_disk_artifacts(client: TestClient, tmp_path):
    task_id = _make_task(client)
    task_dir = _seed_artifact(tmp_path, task_id)
    assert task_dir.exists()

    r = client.delete(f"/api/tasks/{task_id}", headers=_admin())
    assert r.status_code == 200
    assert not task_dir.exists()


def test_batch_delete_removes_disk_artifacts(client: TestClient, tmp_path):
    first = _make_task(client)
    second = _make_task(client)
    for tid in (first, second):
        _seed_artifact(tmp_path, tid)

    r = client.post(
        "/api/tasks/batch-delete",
        json={"task_ids": [first, second]},
        headers=_admin(),
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == 2
    assert not (tmp_path / "artifacts" / first).exists()
    assert not (tmp_path / "artifacts" / second).exists()
