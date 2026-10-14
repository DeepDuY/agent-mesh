"""Public REST paths and field-level permissions of the consolidated API."""

from __future__ import annotations

from fastapi.testclient import TestClient


ADMIN = {"Authorization": "Bearer admin-api-token-123", "X-Agent-Mesh-UI": "1"}
EDGE = {"Authorization": "Bearer mcp-global-token"}


def test_public_routes_are_resource_or_action_paths(client: TestClient):
    schema = client.get("/openapi.json").json()["paths"]
    for canonical in (
        "/api/auth:login", "/api/users", "/api/tasks", "/api/runs",
        "/api/tasks/{task_id}:cancel", "/api/agents/{agent_id}",
        "/api/agents:batch_update", "/api/files:batch_delete",
        "/api/tasks/{task_id}/artifacts", "/api/bootstrap:sync",
    ):
        assert canonical in schema
    for legacy in (
        "/api/auth/login", "/api/auth/users", "/api/tasks/dispatch",
        "/api/tasks/dispatch-batch", "/api/agents/{agent_id}/alias",
        "/api/agents/{agent_id}/description", "/api/agents/{agent_id}/access",
        "/api/agents/{agent_id}/template", "/api/agents/{agent_id}/upgrade",
        "/api/agents/batch/template", "/api/agents/batch/access",
        "/api/files/batch-delete", "/api/bootstrap/sync",
        "/api/artifacts/{task_id}", "/api/skill-pack/agent-mesh",
    ):
        assert legacy not in schema
    assert not any("/batch-" in path for path in schema)
    old = client.get("/api/auth/me", headers=ADMIN)
    assert old.status_code == 200 and old.headers.get("Deprecation") == "true"
    assert client.get("/api/me", headers=ADMIN).headers.get("Deprecation") is None


def test_canonical_dispatch_and_actions_with_colon_device_id(client: TestClient):
    device_id = "00:aa:bb:cc:dd:01"
    poll = client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "node", "device_id": device_id, "os": "linux"},
        headers=EDGE,
    )
    assert poll.status_code == 200
    created = client.post(
        "/api/tasks", json={"agent_id": device_id, "mode": "command", "instruction": "ls"},
        headers=ADMIN,
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    cancelled = client.post(f"/api/tasks/{task_id}:cancel", headers=ADMIN)
    assert cancelled.status_code == 200, cancelled.text
    updated = client.patch(f"/api/agents/{device_id}", json={"alias": "prod"}, headers=ADMIN)
    assert updated.status_code == 200, updated.text
    assert updated.json()["agent"]["alias"] == "prod"
    r = client.post(f"/api/agents/{device_id}:rotate_token", headers=ADMIN)
    assert r.status_code == 200, r.text


def test_merged_agent_patch_does_not_lower_permissions(client: TestClient):
    device_id = "00:aa:bb:cc:dd:01"
    client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "node", "device_id": device_id}, headers=EDGE,
    )
    team = client.post("/api/teams", json={"name": "ops"}, headers=ADMIN).json()["team"]["team_id"]
    made = client.post(
        "/api/users", json={"username": "alice", "password": "secret123", "team_id": team},
        headers=ADMIN,
    ).json()
    assert client.get("/api/users", headers=ADMIN).status_code == 200
    assert client.get("/api/me", headers=ADMIN).status_code == 200
    alice = {"Authorization": f"Bearer {made['token']}", "X-Agent-Mesh-UI": "1"}
    client.patch(f"/api/agents/{device_id}", json={"access": {"users": [made["user_id"]]}}, headers=ADMIN)
    assert client.patch(f"/api/agents/{device_id}", json={"alias": "alice-node"}, headers=alice).status_code == 200
    assert client.patch(f"/api/agents/{device_id}", json={"llm_api_key": "secret"}, headers=alice).status_code == 403
    assert client.patch(f"/api/agents/{device_id}", json={"access": {"users": []}}, headers=alice).status_code == 403
    assert client.post(
        "/api/agents:batch_update",
        json={"agent_ids": [1], "changes": {"access": {"users": []}}}, headers=alice,
    ).status_code == 403
    assert client.post("/api/users/alice:reset_password", json={"password": "othersecret"}, headers=ADMIN).status_code == 200
    assert client.patch(
        f"/api/agents/{device_id}", json={"template_id": None},
        headers={"Authorization": "Bearer admin-api-token-123"},
    ).status_code == 404
    assert client.patch(
        f"/api/agents/{device_id}", json={"template_id": None}, headers=ADMIN,
    ).status_code == 200
    other_team = client.post("/api/teams", json={"name": "other"}, headers=ADMIN).json()["team"]["team_id"]
    bob = client.post(
        "/api/users", json={"username": "bob", "password": "secret123", "team_id": other_team},
        headers=ADMIN,
    ).json()
    client.patch(
        f"/api/agents/{device_id}",
        json={"access": {"users": [made["user_id"], bob["user_id"]]}}, headers=ADMIN,
    )
    created = client.post(
        "/api/tasks", json={"agent_id": device_id, "mode": "command", "instruction": "secret for bob"},
        headers={"Authorization": f"Bearer {bob['token']}"},
    )
    assert created.status_code == 200, created.text
    detail = client.get(f"/api/agents/{device_id}?include=tasks", headers=alice)
    assert detail.status_code == 200 and detail.json()["agent"]["tasks"] == []


def test_canonical_batch_and_download_paths(client: TestClient):
    poll = client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "node", "device_id": "dev-1", "os": "linux", "arch": "x64"},
        headers=EDGE,
    )
    assert poll.status_code == 200
    created = client.post(
        "/api/runs",
        json={"targets": ["dev-1"], "mode": "command", "instruction": "ls"},
        headers=ADMIN,
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    assert client.get(f"/api/runs/{run_id}", headers=ADMIN).json()["total"] == 1
    assert client.get("/api/skill-packs/agent-mesh", headers=ADMIN).status_code == 200
    task_id = created.json()["task_ids"][0]
    uploaded = client.post(
        f"/api/tasks/{task_id}/artifacts",
        files={"files": ("result.txt", b"ok", "text/plain")}, headers=EDGE,
    )
    assert uploaded.status_code == 200, uploaded.text
    artifact = uploaded.json()["artifacts"][0]
    assert artifact["download_url"] == f"/api/tasks/{task_id}/artifacts/{artifact['artifact_id']}"
    assert client.get(artifact["download_url"], headers=ADMIN).content == b"ok"
    assert client.get(f"/api/tasks/{task_id}/artifacts", headers=ADMIN).status_code == 200
    assert client.post(f"/api/runs/{run_id}:cancel", headers=ADMIN).status_code == 200
    agent = client.get("/api/agents/dev-1", headers=ADMIN).json()["agent"]
    updated = client.post(
        "/api/agents:batch_update",
        json={"agent_ids": [agent["id"]], "changes": {"template_id": None}},
        headers=ADMIN,
    )
    assert updated.status_code == 200 and updated.json()["applied"] == 1
