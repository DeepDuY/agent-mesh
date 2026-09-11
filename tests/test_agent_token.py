from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
ADMIN_USER_ID = "u-admin"
GLOBAL_TOKEN = "mcp-global-token"


def _poll(client, device_id="00:aa:bb:cc:dd:01", token=ADMIN_API_TOKEN):
    return client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "node", "device_id": device_id, "runtime": "opencode",
              "hostname": "h", "os": "linux", "arch": "x64", "version": "1.0.0"},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_agent_token_issued_once_and_authenticates(client: TestClient):
    # First registration (user token) -> independent agent token returned once.
    resp = _poll(client)
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_token"]
    agent_token = data["agent_token"]

    # Second poll does NOT re-issue (token already persisted in DB).
    data = _poll(client).json()
    assert "agent_token" not in data

    # The edge now authenticates with its own token.
    resp = _poll(client, token=agent_token)
    assert resp.status_code == 200
    resp = _poll(client, token=agent_token).json()
    assert "agent_token" not in resp

    # The issuing user is recorded as an operator of the machine.
    detail = client.get(
        "/api/agents/00:aa:bb:cc:dd:01/detail",
        headers={"Authorization": f"Bearer {ADMIN_API_TOKEN}"},
    ).json()["agent"]
    assert detail["metadata"]["allowed_users"] == [ADMIN_USER_ID]


def test_agent_token_not_shared_between_agents(client: TestClient):
    t1 = _poll(client, device_id="dev-1").json()["agent_token"]
    t2 = _poll(client, device_id="dev-2").json()["agent_token"]
    assert t1 != t2
    # Each agent token only authenticates its own device.
    assert _poll(client, device_id="dev-1", token=t2).status_code == 403
    assert _poll(client, device_id="dev-2", token=t1).status_code == 403
    assert _poll(client, device_id="dev-1", token=t1).status_code == 200


def test_agent_token_rotation_invalidates_old(client: TestClient):
    old_token = _poll(client).json()["agent_token"]

    resp = client.post(
        "/api/agents/00:aa:bb:cc:dd:01/token",
        headers={"Authorization": f"Bearer {ADMIN_API_TOKEN}"},
    )
    assert resp.status_code == 200
    new_token = resp.json()["token"]
    assert new_token != old_token

    assert _poll(client, token=old_token).status_code == 401
    assert _poll(client, token=new_token).status_code == 200


def test_agent_token_issued_for_global_token_but_no_user(client: TestClient):
    resp = _poll(client, token=GLOBAL_TOKEN)
    assert resp.status_code == 200
    assert resp.json()["agent_token"]
    # Global-token bootstrap does not associate any user.
    detail = client.get(
        "/api/agents/00:aa:bb:cc:dd:01/detail",
        headers={"Authorization": f"Bearer {ADMIN_API_TOKEN}"},
    ).json()["agent"]
    assert detail["metadata"]["allowed_users"] == []
