"""Run / batch control plane: batch dispatch, run monitoring, idempotency,
depends_on_run fan-in, run cancel/retry, artifact hand-off and fan-out caps.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _edge() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _poll(client: TestClient, device: str, running_tasks=None) -> dict:
    body = {
        "agent_id": f"client-{device}",
        "device_id": device,
        "runtime": "opencode",
        "hostname": f"h-{device}",
        "os": "linux",
    }
    if running_tasks is not None:
        body["running_tasks"] = list(running_tasks)
    return client.post("/api/edge/poll_for_task", json=body, headers=_edge()).json()


def _batch(client: TestClient, targets, instruction: str = "x", **kw):
    body = {"targets": targets, "mode": "command", "instruction": instruction, **kw}
    return client.post("/api/tasks/dispatch-batch", json=body, headers=_admin())


def _dispatch(client: TestClient, agent: str, instruction: str = "x", **kw):
    body = {"agent_id": agent, "mode": "command", "instruction": instruction, **kw}
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


def _set(client: TestClient, key: str, value) -> None:
    r = client.patch(
        "/api/settings", json={key: value},
        headers={**_admin(), "X-Agent-Mesh-UI": "1"},
    )
    assert r.status_code == 200, r.text


def test_batch_dispatch_and_run_aggregate(client: TestClient):
    _poll(client, "00:aa:00:00:00:01")
    _poll(client, "00:aa:00:00:00:02")
    r = _batch(client, ["00:aa:00:00:00:01", "00:aa:00:00:00:02"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["queued"] == 2
    run_id = data["run_id"]
    assert run_id.startswith("r-")

    run = client.get(f"/api/runs/{run_id}", headers=_admin()).json()
    assert run["total"] == 2
    assert run["counts"].get("queued") == 2
    assert run["active"] is True
    assert len(run["tasks"]) == 2
    assert all(t["run_id"] == run_id for t in run["tasks"])

    listed = client.get(f"/api/tasks?run_id={run_id}", headers=_admin()).json()
    assert listed["total"] == 2


def test_run_unknown_returns_404(client: TestClient):
    r = client.get("/api/runs/r-nope", headers=_admin())
    assert r.status_code == 404


def test_batch_reports_denied_targets(client: TestClient):
    r = _batch(client, ["does-not-exist"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["queued"] == 0
    assert data["task_ids"] == []
    assert data["denied"][0]["reason"] == "agent not found"


def test_batch_idempotency_key(client: TestClient):
    _poll(client, "00:aa:00:00:00:03")
    first = _batch(client, ["00:aa:00:00:00:03"], idempotency_key="k-1").json()
    second = _batch(client, ["00:aa:00:00:00:03"], idempotency_key="k-1").json()
    assert first["run_id"] == second["run_id"]
    assert second["duplicate"] is True
    run = client.get(f"/api/runs/{first['run_id']}", headers=_admin()).json()
    assert run["total"] == 1


def test_depends_on_run_gates_reduce(client: TestClient):
    dev = "00:aa:00:00:00:04"
    _poll(client, dev)
    batch = _batch(client, [dev], instruction="map").json()
    map_id = batch["task_ids"][0]

    rr = _dispatch(client, dev, instruction="reduce", depends_on_run=batch["run_id"])
    assert rr.status_code == 200, rr.text
    reduce_id = rr.json()["task_id"]

    # Only the map task is handed out; the reduce task waits for the whole run.
    claimed = _poll(client, dev)["tasks"]
    assert [t["task_id"] for t in claimed] == [map_id]

    _finish(client, map_id, "completed")
    claimed = _poll(client, dev, running_tasks=[])["tasks"]
    assert [t["task_id"] for t in claimed] == [reduce_id]


def test_depends_on_run_unknown_rejected(client: TestClient):
    _poll(client, "00:aa:00:00:00:05")
    r = _dispatch(client, "00:aa:00:00:00:05", depends_on_run="r-nope")
    assert r.status_code == 400
    assert "depends_on_run not found" in r.json()["detail"]


def test_run_cancel(client: TestClient):
    dev = "00:aa:00:00:00:06"
    _poll(client, dev)
    data = _batch(client, [dev]).json()
    r = client.post(f"/api/runs/{data['run_id']}/cancel", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] == 1
    task = client.get(f"/api/tasks/{data['task_ids'][0]}", headers=_admin()).json()["task"]
    assert task["status"] == "cancelled"


def test_run_retry_failed(client: TestClient):
    dev = "00:aa:00:00:00:07"
    _poll(client, dev)
    data = _batch(client, [dev]).json()
    tid = data["task_ids"][0]
    _poll(client, dev)  # claim
    _finish(client, tid, "failed")

    r = client.post(f"/api/runs/{data['run_id']}/retry", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["retried"] == 1
    task = client.get(f"/api/tasks/{tid}", headers=_admin()).json()["task"]
    assert task["status"] == "queued"


def test_attachments_from_materializes_artifacts(client: TestClient):
    dev = "00:aa:00:00:00:08"
    _poll(client, dev)
    up = _dispatch(client, dev, instruction="make").json()["task_id"]
    r = client.post(
        f"/api/artifacts/{up}",
        files={"files": ("out.txt", b"hello", "text/plain")},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text

    r = _dispatch(client, dev, instruction="use", attachments_from=[up])
    assert r.status_code == 200, r.text
    down = r.json()["task_id"]
    task = client.get(f"/api/tasks/{down}", headers=_admin()).json()["task"]
    assert len(task["attachments"]) == 1
    att = task["attachments"][0]
    assert att["filename"] == "out.txt"
    assert att["download_url"].startswith("/api/files/")


def test_per_node_fanout_cap(client: TestClient):
    dev = "00:aa:00:00:00:09"
    _poll(client, dev)
    _set(client, "max_run_per_node", 1)
    # Two references to the *same* node resolve to one queue key.
    r = _batch(client, [dev, f"client-{dev}"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["queued"] == 1
    assert len(data["denied"]) == 1
    assert "per-node fan-out" in data["denied"][0]["reason"]


def test_global_fanout_cap(client: TestClient):
    _poll(client, "00:aa:00:00:00:0a")
    _poll(client, "00:aa:00:00:00:0b")
    _set(client, "max_batch_fanout", 1)
    r = _batch(client, ["00:aa:00:00:00:0a", "00:aa:00:00:00:0b"])
    assert r.status_code == 400
    assert "exceeds limit" in r.json()["detail"]
