import sqlite3

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
ADMIN_SESSION_SECRET = "test-session-secret"


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _team_id(client: TestClient) -> str:
    """Return a team id, creating a default team if none exists."""
    teams = client.get("/api/teams", headers=_admin_headers()).json()["teams"]
    if teams:
        return teams[0]["team_id"]
    return client.post(
        "/api/teams", json={"name": "default"}, headers=_admin_headers()
    ).json()["team"]["team_id"]


def _create_user(client: TestClient, username: str, password: str = "secret123", role: str = "user"):
    """Create a user (every user must belong to a team)."""
    return client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "role": role,
            "team_id": _team_id(client),
        },
        headers=_admin_headers(),
    )


def test_login_returns_session_token(client: TestClient):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "admin"
    assert data["role"] == "admin"
    assert data["token_type"] == "session"
    assert data["token"] and data["token"] != ADMIN_API_TOKEN
    # The session token works as a Bearer credential for REST.
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {data['token']}"}).status_code == 200


def test_login_failure(client: TestClient):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


def test_tasks_require_auth(client: TestClient):
    resp = client.get("/api/tasks")
    assert resp.status_code == 401

    resp = client.get("/api/tasks", headers=_admin_headers())
    assert resp.status_code == 200


def test_edge_uses_global_token(client: TestClient):
    resp = client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01", "runtime": "opencode", "hostname": "host", "os": "linux"},
        headers={"Authorization": "Bearer mcp-global-token"},
    )
    assert resp.status_code == 200


def test_dispatch_and_status_with_user_token(client: TestClient):
    headers = _admin_headers()
    # Register the agent first (poll registers + returns queued tasks).
    client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01", "runtime": "opencode", "hostname": "host", "os": "linux"},
        headers={"Authorization": "Bearer mcp-global-token"},
    )
    resp = client.post(
        "/api/tasks/dispatch",
        headers=headers,
        json={"agent_id": "00:aa:bb:cc:dd:01", "mode": "command", "instruction": "echo hello", "workdir": "."},
    )
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    resp = client.get(f"/api/tasks/{task_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["task"]
    assert data["task_id"] == task_id
    assert data["mode"] == "command"


def test_agent_detail_and_alias(client: TestClient):
    headers = _admin_headers()
    # Ensure agent exists via heartbeat edge endpoint.
    client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01", "runtime": "opencode", "hostname": "edge-host", "os": "linux"},
        headers={"Authorization": "Bearer mcp-global-token"},
    )
    resp = client.get("/api/agents/00:aa:bb:cc:dd:01/detail", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent"]["hostname"] == "edge-host"
    assert data["agent"]["display_name"] == "edge-host"

    resp = client.patch(
        "/api/agents/00:aa:bb:cc:dd:01/alias",
        json={"alias": "production-node-1"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["agent"]["alias"] == "production-node-1"
    assert resp.json()["agent"]["display_name"] == "production-node-1"


# ----------------------------------------------------------------------
# User management
# ----------------------------------------------------------------------
def test_create_user_returns_token_once(client: TestClient):
    resp = _create_user(client, "bob")
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "bob"
    assert data["role"] == "user"
    assert data["token_type"] == "api"
    bob_token = data["token"]
    assert bob_token

    # The new user's API token works for REST.
    assert client.get(
        "/api/tasks", headers={"Authorization": f"Bearer {bob_token}"}
    ).status_code == 200

    # No token is disclosed in the listing.
    resp = client.get("/api/auth/users", headers=_admin_headers())
    users = resp.json()["users"]
    bob_row = next(u for u in users if u["username"] == "bob")
    assert "token" not in bob_row
    assert "token_hash" not in bob_row


def test_create_user_requires_admin(client: TestClient):
    # A plain user cannot create users.
    created = _create_user(client, "carol")
    carol_token = created.json()["token"]
    resp = client.post(
        "/api/auth/users",
        json={"username": "dave", "password": "secret123"},
        headers={"Authorization": f"Bearer {carol_token}"},
    )
    assert resp.status_code == 403


def test_create_user_requires_team(client: TestClient):
    # team_id is mandatory.
    resp = client.post(
        "/api/auth/users",
        json={"username": "noteam", "password": "secret123", "role": "user"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400
    assert "team" in resp.json()["detail"]

    # A nonexistent team is rejected too.
    resp = client.post(
        "/api/auth/users",
        json={"username": "noteam", "password": "secret123", "team_id": "team-nope"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400


def test_create_user_duplicate(client: TestClient):
    resp = _create_user(client, "eve")
    assert resp.status_code == 200
    resp = _create_user(client, "eve")
    assert resp.status_code == 409


def test_disabled_user_cannot_access(client: TestClient):
    created = _create_user(client, "mallory")
    mallory_token = created.json()["token"]
    # Simulate disable at the DB level (no dedicated endpoint yet).
    db_path = client.app.state.store.store._db.db_path
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE users SET disabled = 1 WHERE username = 'mallory'")
        conn.commit()
    finally:
        conn.close()
    resp = client.get("/api/tasks", headers={"Authorization": f"Bearer {mallory_token}"})
    assert resp.status_code == 401
    resp = client.post("/api/auth/login", json={"username": "mallory", "password": "secret123"})
    assert resp.status_code == 403


def test_rotate_token_invalidates_old(client: TestClient):
    created = _create_user(client, "frank")
    old_token = created.json()["token"]
    resp = client.post(
        "/api/auth/users/frank/token",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    new_token = resp.json()["token"]
    assert new_token != old_token
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200


def test_delete_user(client: TestClient):
    created = _create_user(client, "trent")
    trent_token = created.json()["token"]
    resp = client.delete("/api/auth/users/trent", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {trent_token}"}).status_code == 401


def test_cannot_delete_admin_or_self(client: TestClient):
    resp = client.delete("/api/auth/users/admin", headers=_admin_headers())
    assert resp.status_code == 400


def test_set_user_password(client: TestClient):
    created = _create_user(client, "ursula")
    ursula_token = created.json()["token"]
    resp = client.post(
        "/api/auth/users/ursula/password",
        json={"password": "newsecret456"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    # Old password no longer logs in.
    assert client.post(
        "/api/auth/login", json={"username": "ursula", "password": "secret123"}
    ).status_code == 401


def test_change_own_password(client: TestClient):
    created = _create_user(client, "victor")
    victor_token = created.json()["token"]
    headers = {"Authorization": f"Bearer {victor_token}"}
    resp = client.post(
        "/api/auth/change-password",
        json={"old_password": "secret123", "new_password": "freshpass99"},
        headers=headers,
    )
    assert resp.status_code == 200
    # Token is unaffected by a password change.
    assert client.get("/api/tasks", headers=headers).status_code == 200
    assert client.post(
        "/api/auth/login", json={"username": "victor", "password": "secret123"}
    ).status_code == 401


# ----------------------------------------------------------------------
# Task management
# ----------------------------------------------------------------------
def _register_and_dispatch(client, headers, instruction="echo hello"):
    client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01", "runtime": "opencode", "hostname": "host", "os": "linux"},
        headers={"Authorization": "Bearer mcp-global-token"},
    )
    resp = client.post(
        "/api/tasks/dispatch",
        headers=headers,
        json={"agent_id": "00:aa:bb:cc:dd:01", "mode": "command", "instruction": instruction, "workdir": "."},
    )
    assert resp.status_code == 200
    return resp.json()["task_id"]


def test_tasks_list_returns_total(client: TestClient):
    headers = _admin_headers()
    _register_and_dispatch(client, headers, "task A")
    _register_and_dispatch(client, headers, "task B")
    resp = client.get("/api/tasks?limit=1&offset=0", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tasks"]) == 1
    assert data["total"] == 2
    assert data["limit"] == 1
    assert data["offset"] == 0


def test_delete_single_task(client: TestClient):
    headers = _admin_headers()
    task_id = _register_and_dispatch(client, headers)
    resp = client.delete(f"/api/tasks/{task_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert client.get(f"/api/tasks/{task_id}", headers=headers).status_code == 404
    assert client.delete(f"/api/tasks/{task_id}", headers=headers).status_code == 404


def test_batch_delete_by_ids(client: TestClient):
    headers = _admin_headers()
    a = _register_and_dispatch(client, headers, "batch A")
    b = _register_and_dispatch(client, headers, "batch B")
    c = _register_and_dispatch(client, headers, "batch C")
    resp = client.post(
        "/api/tasks/batch-delete",
        json={"task_ids": [a, b]},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert client.get(f"/api/tasks/{a}", headers=headers).status_code == 404
    assert client.get(f"/api/tasks/{b}", headers=headers).status_code == 404
    assert client.get(f"/api/tasks/{c}", headers=headers).status_code == 200


def test_batch_delete_all_matching(client: TestClient):
    headers = _admin_headers()
    _register_and_dispatch(client, headers, "all A")
    _register_and_dispatch(client, headers, "all B")
    # No status filter -> deletes everything across all pages.
    resp = client.post(
        "/api/tasks/batch-delete",
        json={"all_matching": True},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert client.get("/api/tasks", headers=headers).json()["total"] == 0


def test_batch_delete_invalid_status(client: TestClient):
    headers = _admin_headers()
    resp = client.post(
        "/api/tasks/batch-delete",
        json={"all_matching": True, "status": "bogus"},
        headers=headers,
    )
    assert resp.status_code == 400
