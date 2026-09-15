"""Edge/artifact authorization: a token may only touch its own task.

Covers report items 3.3 (artifact read leak), 3.8 (edge write/read leak) and
3.10 (artifact write to an unknown task), plus 3.15 (path traversal).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_mesh.orchestrator.artifact_store import ArtifactStore

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEV1 = "00:aa:bb:cc:dd:01"
DEV2 = "00:aa:bb:cc:dd:02"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin() -> dict:
    return _bearer(ADMIN_API_TOKEN)


def _global() -> dict:
    return _bearer(GLOBAL_TOKEN)


def _poll(client: TestClient, device_id: str, token: str = ADMIN_API_TOKEN, agent_id: str = "node"):
    return client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": agent_id,
            "device_id": device_id,
            "runtime": "opencode",
            "hostname": agent_id,
            "os": "linux",
            "arch": "x64",
            "version": "1.0.0",
        },
        headers=_bearer(token),
    )


def _agent_token(client: TestClient, device_id: str, agent_id: str = "node") -> str:
    return _poll(client, device_id, token=GLOBAL_TOKEN, agent_id=agent_id).json()["agent_token"]


def _dispatch(client: TestClient, device_id: str, headers: dict | None = None) -> str:
    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": device_id, "mode": "command", "instruction": "echo hi"},
        headers=headers or _admin(),
    )
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _team(client: TestClient, name: str) -> str:
    return client.post("/api/teams", json={"name": name}, headers=_admin()).json()["team"]["team_id"]


def _user(client: TestClient, username: str):
    team_id = _team(client, f"team-{username}")
    r = client.post(
        "/api/auth/users",
        json={"username": username, "password": "secret123", "role": "user", "team_id": team_id},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    d = r.json()
    return d["user_id"], _bearer(d["token"])


def test_agent_cannot_touch_another_agents_task(client: TestClient):
    t1 = _agent_token(client, DEV1, "node1")
    t2 = _agent_token(client, DEV2, "node2")
    task = _dispatch(client, DEV1)
    # agent1 claims its own task
    assert _poll(client, DEV1, token=t1, agent_id="node1").status_code == 200

    # agent2's token is denied on every read/write of agent1's task.
    assert client.post(
        "/api/edge/mark_started", json={"task_id": task}, headers=_bearer(t2)
    ).status_code == 403
    assert client.post(
        "/api/edge/task_log",
        json={"task_id": task, "entries": [{"kind": "text", "content": "x"}]},
        headers=_bearer(t2),
    ).status_code == 403
    assert client.post(
        "/api/edge/get_task_status", json={"task_id": task}, headers=_bearer(t2)
    ).json() == {"found": False}
    assert client.post(
        "/api/edge/submit_result",
        json={"task_id": task, "status": "completed"},
        headers=_bearer(t2),
    ).status_code == 403
    assert client.post(
        f"/api/artifacts/{task}",
        files={"files": ("x.txt", b"x", "text/plain")},
        headers=_bearer(t2),
    ).status_code == 403

    # agent1's token is allowed.
    assert client.post(
        "/api/edge/mark_started", json={"task_id": task}, headers=_bearer(t1)
    ).json()["accepted"] is True
    assert client.post(
        "/api/edge/task_log",
        json={"task_id": task, "entries": [{"kind": "text", "content": "x"}]},
        headers=_bearer(t1),
    ).json()["accepted"] is True
    assert client.post(
        "/api/edge/get_task_status", json={"task_id": task}, headers=_bearer(t1)
    ).json()["found"] is True
    assert client.post(
        f"/api/artifacts/{task}",
        files={"files": ("x.txt", b"x", "text/plain")},
        headers=_bearer(t1),
    ).status_code == 200


def test_artifacts_scoped_to_task_owner(client: TestClient):
    _poll(client, DEV1)  # register the node
    uid, h_a = _user(client, "alice")
    client.patch(f"/api/agents/{DEV1}/access", json={"users": [uid]}, headers=_admin())
    task_a = _dispatch(client, DEV1, headers=h_a)

    # An admin-owned task with one artifact (uploaded via the global token).
    task_admin = _dispatch(client, DEV1)
    up = client.post(
        f"/api/artifacts/{task_admin}",
        files={"files": ("secret.txt", b"s", "text/plain")},
        headers=_global(),
    )
    assert up.status_code == 200

    # alice cannot list/admin's task nor download its artifact.
    assert client.get(f"/api/artifacts/{task_admin}", headers=h_a).status_code == 404
    aid = up.json()["artifacts"][0]["artifact_id"]
    assert client.get(f"/api/artifacts/{task_admin}/{aid}", headers=h_a).status_code == 404

    # alice's own task artifacts are visible to her.
    client.post(
        f"/api/artifacts/{task_a}",
        files={"files": ("mine.txt", b"m", "text/plain")},
        headers=_global(),
    )
    own = client.get(f"/api/artifacts/{task_a}", headers=h_a)
    assert own.status_code == 200 and len(own.json()["artifacts"]) == 1

    # admin sees everything.
    assert client.get(f"/api/artifacts/{task_admin}", headers=_admin()).status_code == 200


def test_upload_to_unknown_task_rejected(client: TestClient):
    r = client.post(
        "/api/artifacts/does-not-exist",
        files={"files": ("x.txt", b"x", "text/plain")},
        headers=_global(),
    )
    assert r.status_code == 404


def test_artifact_store_rejects_path_traversal(tmp_path):
    store = ArtifactStore(str(tmp_path))
    assert store.list("../escape") == []
    assert store.resolve("../escape", "a-1") is None
    with pytest.raises(ValueError):
        store.save("../escape", "x.txt", b"x")
    # No stray directory was created outside base_dir.
    assert not (tmp_path.parent / "escape").exists()
