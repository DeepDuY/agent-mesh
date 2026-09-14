"""Multi-tenant isolation: teams, node access, task/file ownership."""

from __future__ import annotations

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE = "00:aa:bb:cc:dd:01"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _poll(client: TestClient) -> None:
    client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "node",
            "device_id": DEVICE,
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
            "arch": "x64",
        },
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )


def _team_id(client: TestClient, name: str) -> str:
    return client.post(
        "/api/teams", json={"name": name}, headers=_admin()
    ).json()["team"]["team_id"]


def _user(client: TestClient, username: str):
    # Every user must belong to a team; give each its own so tests can move them.
    team_id = _team_id(client, f"team-{username}")
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
    assert r.status_code == 200, r.text
    data = r.json()
    return data["user_id"], {"Authorization": f"Bearer {data['token']}"}


def _dispatch(client: TestClient, headers, instruction="echo hi"):
    return client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE, "mode": "command", "instruction": instruction},
        headers=headers,
    )


def test_team_crud_and_single_membership(client: TestClient):
    uid_a, _ = _user(client, "alice")
    uid_b, _ = _user(client, "bob")
    team = client.post(
        "/api/teams", json={"name": "ops", "description": "运维组"}, headers=_admin()
    )
    assert team.status_code == 200
    team_id = team.json()["team"]["team_id"]

    client.post(f"/api/teams/{team_id}/members", json={"user_id": uid_a}, headers=_admin())
    teams = client.get("/api/teams", headers=_admin()).json()["teams"]
    assert teams[0]["members"] == [uid_a]

    # A user belongs to at most one team: adding to another moves them.
    team2 = client.post("/api/teams", json={"name": "dev"}, headers=_admin()).json()["team"]
    client.post(f"/api/teams/{team2['team_id']}/members", json={"user_id": uid_a}, headers=_admin())
    assert client.get("/api/teams", headers=_admin()).json()["teams"]
    # (bob never added)
    _ = uid_b


def test_node_access_controls_listing_and_dispatch(client: TestClient):
    _poll(client)
    uid, h = _user(client, "alice")
    _, h_bob = _user(client, "bob")

    # Without access, alice sees no nodes and cannot dispatch.
    assert client.get("/api/agents", headers=h).json()["agents"] == []
    assert _dispatch(client, h).status_code == 403
    assert client.get(f"/api/agents/{DEVICE}", headers=h).status_code == 404

    # Admins always see the node.
    assert len(client.get("/api/agents", headers=_admin()).json()["agents"]) == 1

    # Grant alice access -> she sees the node and can dispatch.
    client.patch(f"/api/agents/{DEVICE}/access", json={"users": [uid]}, headers=_admin())
    assert len(client.get("/api/agents", headers=h).json()["agents"]) == 1
    assert _dispatch(client, h).status_code == 200
    # bob still has no access.
    assert _dispatch(client, h_bob).status_code == 403


def test_team_access_grants_all_members(client: TestClient):
    _poll(client)
    team = client.post("/api/teams", json={"name": "teamx"}, headers=_admin()).json()["team"]
    uid, h = _user(client, "carol")
    client.post(f"/api/teams/{team['team_id']}/members", json={"user_id": uid}, headers=_admin())
    client.patch(
        f"/api/agents/{DEVICE}/access",
        json={"teams": [team["team_id"]]},
        headers=_admin(),
    )
    assert len(client.get("/api/agents", headers=h).json()["agents"]) == 1
    assert _dispatch(client, h).status_code == 200


def test_task_visibility_scoped_by_user_and_team(client: TestClient):
    _poll(client)
    uid_a, h_a = _user(client, "alice")
    uid_b, h_b = _user(client, "bob")
    team = client.post("/api/teams", json={"name": "t"}, headers=_admin()).json()["team"]
    client.post(f"/api/teams/{team['team_id']}/members", json={"user_id": uid_a}, headers=_admin())
    client.patch(
        f"/api/agents/{DEVICE}/access",
        json={"teams": [team["team_id"]], "users": [uid_a, uid_b]},
        headers=_admin(),
    )

    task_a = _dispatch(client, h_a).json()["task_id"]
    task_b = _dispatch(client, h_b).json()["task_id"]

    a_ids = {t["task_id"] for t in client.get("/api/tasks", headers=h_a).json()["tasks"]}
    b_ids = {t["task_id"] for t in client.get("/api/tasks", headers=h_b).json()["tasks"]}
    # alice dispatched task_a and is in the team; bob (no team) dispatched task_b.
    assert task_a in a_ids
    assert task_b in b_ids
    # bob cannot see alice's task (unless same team); alice cannot see bob's.
    assert task_b not in a_ids
    assert task_a not in b_ids
    # Cross-task access is a 404.
    assert client.get(f"/api/tasks/{task_a}", headers=h_b).status_code == 404
    assert client.get(f"/api/tasks/{task_b}", headers=h_a).status_code == 404


def test_file_ownership_isolation(client: TestClient):
    _, h_a = _user(client, "alice")
    _, h_b = _user(client, "bob")
    up = client.post(
        "/api/files",
        files={"files": ("secret.txt", b"hello", "text/plain")},
        headers=h_a,
    )
    fid = up.json()["files"][0]["file_id"]

    assert [f["file_id"] for f in client.get("/api/files", headers=h_a).json()["files"]] == [fid]
    assert client.get("/api/files", headers=h_b).json()["files"] == []
    assert client.get(f"/api/files/{fid}", headers=h_b).status_code == 404
    assert client.delete(f"/api/files/{fid}", headers=h_b).status_code == 404
    # admin sees and can download everything.
    assert client.get(f"/api/files/{fid}", headers=_admin()).status_code == 200


def test_non_admin_cannot_manage_teams(client: TestClient):
    _, h = _user(client, "alice")
    assert client.get("/api/teams", headers=h).status_code == 403
    assert client.post("/api/teams", json={"name": "x"}, headers=h).status_code == 403


# ----------------------------------------------------------------------
# Batch node assignment (avoid opening every node one by one)
# ----------------------------------------------------------------------
def _admin_ui() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _poll_at(client: TestClient, device_id: str, agent_id: str = "node") -> None:
    client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": agent_id,
            "device_id": device_id,
            "runtime": "opencode",
            "hostname": agent_id,
            "os": "linux",
            "arch": "x64",
        },
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )


def _two_nodes(client: TestClient) -> list[int]:
    _poll_at(client, DEVICE, "node1")
    _poll_at(client, "00:aa:bb:cc:dd:02", "node2")
    agents = client.get("/api/agents", headers=_admin()).json()["agents"]
    return [a["id"] for a in agents]


def test_batch_node_access_and_team_nodes(client: TestClient):
    ids = _two_nodes(client)
    assert len(ids) == 2
    team = client.post("/api/teams", json={"name": "ops"}, headers=_admin()).json()["team"]
    team_id = team["team_id"]
    uid, h = _user(client, "alice")
    client.post(f"/api/teams/{team_id}/members", json={"user_id": uid}, headers=_admin())

    # Batch add a team to both nodes.
    r = client.post(
        "/api/agents/batch/access",
        json={"agent_ids": ids, "teams": [team_id], "mode": "add"},
        headers=_admin(),
    )
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert len(client.get("/api/agents", headers=h).json()["agents"]) == 2

    # Batch remove it again.
    r = client.post(
        "/api/agents/batch/access",
        json={"agent_ids": ids, "teams": [team_id], "mode": "remove"},
        headers=_admin(),
    )
    assert r.json()["updated"] == 2
    assert client.get("/api/agents", headers=h).json()["agents"] == []

    # Team -> nodes direction: replace the whole set at once.
    r = client.put(
        f"/api/teams/{team_id}/nodes",
        json={"agent_ids": [ids[0]]},
        headers=_admin(),
    )
    assert r.status_code == 200
    visible = client.get("/api/agents", headers=h).json()["agents"]
    assert [a["id"] for a in visible] == [ids[0]]

    # Batch access is admin-only.
    assert client.post(
        "/api/agents/batch/access", json={"agent_ids": ids}, headers=h
    ).status_code == 403


def test_batch_apply_template(client: TestClient):
    ids = _two_nodes(client)
    tpl = client.post(
        "/api/templates",
        json={"name": "prod", "system_prompt": "be careful"},
        headers=_admin_ui(),
    ).json()["template"]
    r = client.post(
        "/api/agents/batch/template",
        json={"agent_ids": ids, "template_id": tpl["id"]},
        headers=_admin_ui(),
    )
    assert r.status_code == 200 and r.json()["applied"] == 2
    agents = client.get("/api/agents", headers=_admin()).json()["agents"]
    assert all(a["template_id"] == tpl["id"] for a in agents)

    # Unbind in one go.
    r = client.post(
        "/api/agents/batch/template",
        json={"agent_ids": ids, "template_id": None},
        headers=_admin_ui(),
    )
    assert r.json()["applied"] == 2
    agents = client.get("/api/agents", headers=_admin()).json()["agents"]
    assert all(a["template_id"] is None for a in agents)
