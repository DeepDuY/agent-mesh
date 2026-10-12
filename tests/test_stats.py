"""Multi-dimensional overview dashboard: aggregation, tenancy scoping, ranges."""

from __future__ import annotations

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE_A = "00:aa:bb:cc:dd:01"
DEVICE_B = "00:aa:bb:cc:dd:02"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _poll(client: TestClient, device: str, agent_id: str = "node") -> None:
    client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": agent_id,
            "device_id": device,
            "runtime": "opencode",
            "hostname": agent_id,
            "os": "linux",
            "arch": "x64",
        },
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )


def _user(client: TestClient, username: str):
    team_id = client.post(
        "/api/teams", json={"name": f"team-{username}"}, headers=_admin()
    ).json()["team"]["team_id"]
    r = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": "secret123",
            "role": "user",
            "team_id": team_id,
        },
        headers=_admin(),
    )
    data = r.json()
    return data["user_id"], team_id, {"Authorization": f"Bearer {data['token']}"}


def _dispatch(client: TestClient, headers, agent: str = DEVICE_A) -> str:
    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": agent, "mode": "command", "instruction": "echo hi"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _finish(client: TestClient, task_id: str, status: str) -> None:
    r = client.post(
        "/api/edge/submit_result",
        json={
            "task_id": task_id,
            "status": status,
            "exit_code": 0 if status == "completed" else 1,
            "summary": status,
            "stdout_tail": "",
        },
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True, r.text


def test_overview_summary_and_breakdowns(client: TestClient):
    _poll(client, DEVICE_A, "node1")
    _poll(client, DEVICE_B, "node2")

    # A task must be assigned to a node before its result is accepted, so poll
    # the target node between dispatch and submit (the normal edge flow).
    done = _dispatch(client, _admin(), DEVICE_A)
    _poll(client, DEVICE_A, "node1")
    _finish(client, done, "completed")
    failed = _dispatch(client, _admin(), DEVICE_B)
    _poll(client, DEVICE_B, "node2")
    _finish(client, failed, "failed")
    _dispatch(client, _admin(), DEVICE_A)  # stays queued

    data = client.get("/api/stats/overview?range=7d", headers=_admin()).json()

    s = data["summary"]
    assert s["total"] == 3
    assert s["completed"] == 1
    assert s["failed"] == 1
    assert s["queued"] == 1
    assert s["success_rate"] == 0.5
    assert s["avg_duration_ms"] is not None

    assert data["by_status"]["completed"] == 1
    assert data["by_status"]["failed"] == 1
    assert data["by_mode"]["command"] == 3

    # Per-node: node1 has 2, node2 has 1 (failed).
    by_node = {r["agent_id"]: r for r in data["by_agent"]}
    assert by_node[DEVICE_A]["total"] == 2
    assert by_node[DEVICE_B]["total"] == 1
    assert by_node[DEVICE_B]["failed"] == 1

    # Per-user: everything was dispatched by the admin account.
    assert data["by_user"][0]["user"] == "admin"
    assert data["by_user"][0]["total"] == 3

    # Hourly heatmap buckets cover every task in the window.
    assert len(data["heatmap"]) >= 1
    assert sum(h["total"] for h in data["heatmap"]) == 3
    assert all("T" in h["bucket"] for h in data["heatmap"])

    assert data["agents"]["total"] == 2
    assert data["agents"]["online"] == 2


def test_overview_team_isolation(client: TestClient):
    _poll(client, DEVICE_A, "node1")
    uid, team_id, h_alice = _user(client, "alice")
    _, _, h_bob = _user(client, "bob")
    client.patch(
        f"/api/agents/{DEVICE_A}/access",
        json={"teams": [team_id]},
        headers=_admin(),
    )

    _dispatch(client, h_alice, DEVICE_A)

    alice = client.get("/api/stats/overview?range=7d", headers=h_alice).json()
    assert alice["summary"]["total"] == 1
    assert {t["team_id"] for t in alice["by_team"]} == {team_id}
    # Entities outside alice's scope are filtered out.
    assert {r["user"] for r in alice["by_user"]} == {"alice"}
    assert {r["agent_id"] for r in alice["by_agent"]} == {DEVICE_A}
    # alice sees the one node she has access to.
    assert alice["agents"]["total"] == 1

    bob = client.get("/api/stats/overview?range=7d", headers=h_bob).json()
    assert bob["summary"]["total"] == 0
    assert bob["by_team"] == []
    assert bob["agents"]["total"] == 0

    admin = client.get("/api/stats/overview?range=7d", headers=_admin()).json()
    assert admin["summary"]["total"] >= 1
    assert team_id in {t["team_id"] for t in admin["by_team"]}


def test_overview_range_validation(client: TestClient):
    assert client.get("/api/stats/overview?range=nope", headers=_admin()).status_code == 400
    assert client.get("/api/stats/overview?range=bogus", headers=_admin()).status_code == 400
    # custom requires an explicit start bound.
    assert (
        client.get("/api/stats/overview?range=custom", headers=_admin()).status_code == 400
    )
    ok = client.get(
        "/api/stats/overview?range=custom&created_after=2000-01-01T00:00:00Z",
        headers=_admin(),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["range"]["preset"] == "custom"
