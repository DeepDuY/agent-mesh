"""`depends_on`: a task is only dispatched once its dependencies completed.

A stopped dependency (failed/timed out/cancelled/denied) cancels the dependent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE = "00:aa:bb:cc:dd:01"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _edge() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _poll(client: TestClient, running_tasks=None) -> dict:
    body = {
        "agent_id": "client",
        "device_id": DEVICE,
        "runtime": "opencode",
        "hostname": "h",
        "os": "linux",
    }
    if running_tasks is not None:
        body["running_tasks"] = list(running_tasks)
    return client.post("/api/edge/poll_for_task", json=body, headers=_edge()).json()


def _dispatch(client: TestClient, instruction: str = "x", depends_on=None):
    body = {"agent_id": DEVICE, "mode": "command", "instruction": instruction}
    if depends_on is not None:
        body["depends_on"] = depends_on
    return client.post("/api/tasks/dispatch", json=body, headers=_admin())


def _finish(client: TestClient, task_id: str, status: str = "completed") -> None:
    r = client.post(
        "/api/edge/submit_result",
        json={
            "task_id": task_id,
            "status": status,
            "exit_code": 0 if status == "completed" else 1,
            "summary": status,
            "stdout_tail": "",
        },
        headers=_edge(),
    )
    assert r.status_code == 200, r.text


def test_dependent_waits_for_dependency(client: TestClient):
    _poll(client)  # register
    dep = _dispatch(client, "first").json()["task_id"]
    dependent = _dispatch(client, "second", depends_on=[dep]).json()["task_id"]

    # First poll only hands out the dependency.
    claimed = _poll(client)["tasks"]
    assert [t["task_id"] for t in claimed] == [dep]

    # The dependent stays queued and exposes its dependency.
    task = client.get(f"/api/tasks/{dependent}", headers=_admin()).json()["task"]
    assert task["depends_on"] == [dep]
    assert task["status"] == "queued"

    # Once the dependency completes, the dependent is claimed.
    _finish(client, dep, "completed")
    claimed = _poll(client, running_tasks=[])["tasks"]
    assert [t["task_id"] for t in claimed] == [dependent]


def test_failed_dependency_cancels_dependent(client: TestClient):
    _poll(client)
    dep = _dispatch(client, "first").json()["task_id"]
    dependent = _dispatch(client, "second", depends_on=[dep]).json()["task_id"]

    _poll(client)  # claim dep
    _finish(client, dep, "failed")

    # The dependent is cancelled, not dispatched.
    claimed = _poll(client, running_tasks=[])["tasks"]
    assert claimed == []
    task = client.get(f"/api/tasks/{dependent}", headers=_admin()).json()["task"]
    assert task["status"] == "cancelled"


def test_unknown_dependency_rejected(client: TestClient):
    _poll(client)
    r = _dispatch(client, "x", depends_on=["t-does-not-exist"])
    assert r.status_code == 400
    assert "dependency task not found" in r.json()["detail"]


@pytest.mark.asyncio
async def test_dependency_cycle_rejected(tmp_path):
    from agent_mesh.orchestrator.store.sqlite import SQLiteStore
    from agent_mesh.orchestrator.task_store import TaskStore
    from agent_mesh.shared.schemas import Task

    backend = SQLiteStore(str(tmp_path / "agent-mesh.db"))
    await backend.initialize()
    try:
        store = TaskStore(store=backend)
        await store.heartbeat("client", DEVICE, "opencode", "h", telemetry={"os": "linux"})
        # Simulate a hand-edited DB where two tasks depend on each other.
        await backend.create_task(
            Task(task_id="t-1", agent_id=DEVICE, instruction="a", depends_on=["t-2"])
        )
        await backend.create_task(
            Task(task_id="t-2", agent_id=DEVICE, instruction="b", depends_on=["t-1"])
        )
        with pytest.raises(ValueError, match="cycle"):
            await store._validate_dependencies(["t-1"])
    finally:
        await backend.close()
