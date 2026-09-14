"""Node templates: CRUD, binding, and delete-unbinds semantics."""

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE = "00:aa:bb:cc:dd:01"


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _register(client: TestClient) -> None:
    client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "node",
            "device_id": DEVICE,
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
        },
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )


def _create(client: TestClient, **body) -> dict:
    payload = {"name": "tpl", **body}
    resp = client.post("/api/templates", json=payload, headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()["template"]


def test_template_crud(client: TestClient):
    permission = {"edit": "deny", "bash": {"*": "deny", "ls *": "allow"}}
    tpl = _create(
        client,
        name="ops",
        description="deploy node",
        node_description="生产部署机",
        system_prompt="你是部署专员",
        llm_model="anthropic/deepseek-v4-pro",
        permission=permission,
    )
    assert tpl["name"] == "ops"
    assert tpl["system_prompt"] == "你是部署专员"
    assert tpl["llm_model"] == "anthropic/deepseek-v4-pro"
    assert tpl["permission"] == permission
    assert tpl["node_description"] == "生产部署机"

    listed = client.get("/api/templates", headers=_admin_headers()).json()["templates"]
    assert "ops" in [t["name"] for t in listed]
    # The three built-in permission templates are always seeded.
    assert {"build", "plan", "readonly"} <= {t["name"] for t in listed}

    got = client.get(f"/api/templates/{tpl['id']}", headers=_admin_headers()).json()["template"]
    assert got["id"] == tpl["id"]

    patched = client.patch(
        f"/api/templates/{tpl['id']}",
        json={"system_prompt": "新提示词", "llm_model": None},
        headers=_admin_headers(),
    ).json()["template"]
    assert patched["system_prompt"] == "新提示词"
    assert patched["llm_model"] is None

    deleted = client.delete(f"/api/templates/{tpl['id']}", headers=_admin_headers())
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/templates/{tpl['id']}", headers=_admin_headers()).status_code == 404


def test_template_name_validation_and_duplicate(client: TestClient):
    resp = client.post(
        "/api/templates", json={"name": "bad name"}, headers=_admin_headers()
    )
    assert resp.status_code == 400

    _create(client, name="dup")
    resp = client.post(
        "/api/templates", json={"name": "dup"}, headers=_admin_headers()
    )
    assert resp.status_code == 409


def test_bind_template_and_delete_unbinds(client: TestClient):
    _register(client)
    tpl = _create(client, name="binding")

    resp = client.patch(
        f"/api/agents/{DEVICE}/template",
        json={"template_id": tpl["id"]},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["agent"]["template_id"] == tpl["id"]

    detail = client.get(f"/api/agents/{DEVICE}/detail", headers=_admin_headers()).json()["agent"]
    assert detail["template_id"] == tpl["id"]

    # A non-existent template is rejected.
    assert client.patch(
        f"/api/agents/{DEVICE}/template",
        json={"template_id": 99999},
        headers=_admin_headers(),
    ).status_code == 404

    # Deleting the template nulls the node binding (FK ON DELETE SET NULL).
    client.delete(f"/api/templates/{tpl['id']}", headers=_admin_headers())
    agent = client.get(f"/api/agents/{DEVICE}", headers=_admin_headers()).json()["agent"]
    assert agent["template_id"] is None
