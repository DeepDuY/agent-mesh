"""Direction 2: agent description, node system prompt, configurable model list."""

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE = "00:aa:bb:cc:dd:01"


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _edge_headers() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _poll(client: TestClient):
    return client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "node",
            "device_id": DEVICE,
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
            "arch": "x64",
            "version": "1.4.2",
        },
        headers=_edge_headers(),
    ).json()


# ----------------------------------------------------------------------
# Node description
# ----------------------------------------------------------------------
def test_agent_description_set_and_visible(client: TestClient):
    _poll(client)
    resp = client.patch(
        f"/api/agents/{DEVICE}/description",
        json={"description": "production web server"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["agent"]["description"] == "production web server"

    agent = client.get(f"/api/agents/{DEVICE}", headers=_admin_headers()).json()["agent"]
    assert agent["description"] == "production web server"

    # Clearing works.
    client.patch(
        f"/api/agents/{DEVICE}/description",
        json={"description": None},
        headers=_admin_headers(),
    )
    agent = client.get(f"/api/agents/{DEVICE}", headers=_admin_headers()).json()["agent"]
    assert agent["description"] is None


# ----------------------------------------------------------------------
# Node system prompt + config sync
# ----------------------------------------------------------------------
def test_node_system_prompt_synced_via_poll(client: TestClient):
    _poll(client)
    resp = client.patch(
        f"/api/agents/{DEVICE}/system_prompt",
        json={"system_prompt": "你是运维专员。"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200

    poll = _poll(client)
    assert poll["config"]["system_prompt"] == "你是运维专员。"
    # A change bumps config_version so the edge re-syncs.
    assert poll["config_version"] == "1"


def test_llm_models_setting_synced(client: TestClient):
    _poll(client)
    resp = client.patch(
        "/api/settings",
        json={"llm_models": "anthropic/m1\nanthropic/m2"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    poll = _poll(client)
    assert poll["config_version"] == "1"
    assert "anthropic/m1" in poll["config"]["llm_models"]


def test_template_prompt_and_model_compose(client: TestClient):
    _poll(client)
    created = client.post(
        "/api/templates",
        json={
            "name": "ops",
            "system_prompt": "template role",
            "llm_model": "anthropic/deepseek-v4-pro",
        },
        headers=_admin_headers(),
    )
    assert created.status_code == 200
    tpl_id = created.json()["template"]["id"]

    resp = client.patch(
        f"/api/agents/{DEVICE}/template",
        json={"template_id": tpl_id},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["agent"]["template_id"] == tpl_id

    poll = _poll(client)
    # Template supplies the model when the node has none.
    assert poll["config"]["llm_model"] == "anthropic/deepseek-v4-pro"
    assert poll["config"]["system_prompt"] == "template role"

    # Node prompt is prepended (built-in + node + template).
    client.patch(
        f"/api/agents/{DEVICE}/system_prompt",
        json={"system_prompt": "node role"},
        headers=_admin_headers(),
    )
    poll = _poll(client)
    sp = poll["config"]["system_prompt"]
    assert sp.index("node role") < sp.index("template role")

    # Node model overrides the template.
    client.patch(
        f"/api/agents/{DEVICE}/llm_config",
        json={"llm_model": "anthropic/deepseek-v4-flash"},
        headers=_admin_headers(),
    )
    poll = _poll(client)
    assert poll["config"]["llm_model"] == "anthropic/deepseek-v4-flash"


# ----------------------------------------------------------------------
# Model allow-list + force configuration
# ----------------------------------------------------------------------
def _dispatch(client, **extra):
    body = {"agent_id": DEVICE, "mode": "llm", "instruction": "hi"}
    body.update(extra)
    return client.post("/api/tasks/dispatch", json=body, headers=_admin_headers())


def test_llm_dispatch_requires_configured_model(client: TestClient):
    _poll(client)
    # Clear the default model -> llm dispatch must be rejected.
    client.patch("/api/settings", json={"llm_model": ""}, headers=_admin_headers())
    resp = _dispatch(client)
    assert resp.status_code == 400
    assert "no LLM model configured" in resp.json()["detail"]

    # Passing an explicit model works even with no default.
    resp = _dispatch(client, model="anthropic/deepseek-v4-flash")
    assert resp.status_code == 200


def test_llm_dispatch_rejects_model_not_in_allow_list(client: TestClient):
    _poll(client)
    resp = _dispatch(client, model="anthropic/not-real")
    assert resp.status_code == 400
    assert "not in allowed list" in resp.json()["detail"]

    resp = _dispatch(client, model="anthropic/deepseek-v4-pro")
    assert resp.status_code == 200


def test_command_dispatch_ignores_model_validation(client: TestClient):
    _poll(client)
    client.patch("/api/settings", json={"llm_model": ""}, headers=_admin_headers())
    resp = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE, "mode": "command", "instruction": "echo hi"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200


def test_list_agents_stable_order(client: TestClient):
    def poll(device: str) -> None:
        client.post(
            "/api/edge/poll_for_task",
            json={
                "agent_id": "node",
                "device_id": device,
                "runtime": "opencode",
                "hostname": device,
                "os": "linux",
            },
            headers=_edge_headers(),
        )

    poll("dev-a")
    poll("dev-b")
    order1 = [a["id"] for a in client.get("/api/agents", headers=_admin_headers()).json()["agents"]]
    # Heartbeats bump `updated_at`; the list order must not change.
    poll("dev-b")
    poll("dev-a")
    order2 = [a["id"] for a in client.get("/api/agents", headers=_admin_headers()).json()["agents"]]
    assert order1 == order2 == sorted(order1)

