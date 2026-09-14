"""Unified permission model (template-level, llm + command shared)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_mesh.edge.config_writer import (
    apply_llm_config,
    build_opencode_config,
    read_permission,
)
from agent_mesh.edge.execution.command import run_command
from agent_mesh.shared import permissions
from agent_mesh.shared.schemas import Task

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE = "00:aa:bb:cc:dd:01"


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _poll(client: TestClient) -> dict:
    return client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "node",
            "device_id": DEVICE,
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
            "arch": "x64",
            "version": "1.5.0",
        },
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    ).json()


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
def test_glob_match():
    assert permissions.glob_match("ls *", "ls -la")
    assert permissions.glob_match("*", "anything")
    assert permissions.glob_match("git status", "git status")
    assert permissions.glob_match("git status *", "git status --porcelain")
    assert not permissions.glob_match("git status", "git status --porcelain")
    assert not permissions.glob_match("rm *", "ls")
    assert permissions.glob_match("a?c", "abc")
    assert not permissions.glob_match("a?c", "abbc")


def test_rule_order_last_match_wins():
    rules = {"*": "deny", "ls *": "allow", "rm *": "deny"}
    assert permissions.evaluate_rules(rules, "ls -la") == "allow"
    assert permissions.evaluate_rules(rules, "rm -rf /") == "deny"
    assert permissions.evaluate_rules(rules, "cat x") == "deny"
    # Shorthand string is a catch-all.
    assert permissions.evaluate_rules("allow", "whatever") == "allow"


def test_profiles_shape():
    assert permissions.expand_profile("build") == {"*": "allow"}
    assert permissions.expand_profile("readonly")["*"] == "deny"
    assert permissions.expand_profile("plan")["edit"] == "deny"
    assert permissions.default_permission()["*"] == "deny"  # readonly
    # expand_profile returns a copy, not the shared object.
    p = permissions.expand_profile("readonly")
    p["*"] = "allow"
    assert permissions.READONLY["*"] == "deny"


def test_evaluate_command_whitelist_and_blacklist():
    whitelist = {"bash": {"*": "deny", "ls *": "allow", "git status *": "allow"}}
    assert permissions.evaluate_command(whitelist, "ls -la") == "allow"
    assert permissions.evaluate_command(whitelist, "rm -rf /") == "deny"
    # One denied sub-command rejects the whole chain.
    assert permissions.evaluate_command(whitelist, "ls -la && rm -rf /") == "deny"

    blacklist = {"bash": {"*": "allow", "rm -rf *": "deny"}}
    assert permissions.evaluate_command(blacklist, "echo hi") == "allow"
    assert permissions.evaluate_command(blacklist, "rm -rf /; echo done") == "deny"

    # Top-level "*" governs when there is no bash rule.
    assert permissions.evaluate_command({"*": "deny"}, "ls") == "deny"
    assert permissions.evaluate_command({"*": "allow"}, "ls") == "allow"
    assert permissions.evaluate_command(None, "ls") == "deny"  # no policy -> ask -> deny


def test_build_opencode_config_embeds_permission():
    cfg = build_opencode_config(
        api_key="k",
        base_url="https://x/v1",
        model="anthropic/m1",
        permission=permissions.expand_profile("readonly"),
        models=["anthropic/m1"],
    )
    assert cfg["permission"]["*"] == "deny"
    assert cfg["permission"]["read"] == "allow"
    # No leaked hardcoded allow-all permission.
    assert cfg["permission"].get("bash") is None


def test_apply_llm_config_persists_permission(tmp_path):
    install = tmp_path / "agent"
    (install / "etc").mkdir(parents=True)
    (install / "etc" / "edge.env").write_text("EDGE_AGENT_ID=node\n", encoding="utf-8")
    apply_llm_config(
        str(install),
        {"llm_api_key": "k", "permission": {"*": "allow"}},
        "3",
    )
    assert read_permission(str(install)) == {"*": "allow"}
    # read_permission returns None when never synced.
    install2 = tmp_path / "agent2"
    (install2 / "etc").mkdir(parents=True)
    assert read_permission(str(install2)) is None


@pytest.mark.asyncio
async def test_command_denied_by_edge_policy(tmp_path):
    task = Task(
        task_id="t-deny",
        agent_id="node",
        mode="command",
        instruction="rm -rf /",
    )
    entries: list[dict] = []

    async def _cb(items):
        entries.extend(items)

    outcome = await run_command(
        task, tmp_path, log_callback=_cb, permission=permissions.expand_profile("readonly")
    )
    assert outcome.exit_code != 0
    assert "权限被拒绝" in outcome.stderr_tail
    assert any(e["kind"] == "error" for e in entries)


# ----------------------------------------------------------------------
# Orchestrator: seeded templates + config-sync
# ----------------------------------------------------------------------
def test_default_templates_seeded(client: TestClient):
    templates = client.get("/api/templates", headers=_admin_headers()).json()["templates"]
    by_name = {t["name"]: t for t in templates}
    assert {"build", "plan", "readonly"} <= set(by_name)
    assert by_name["build"]["permission"] == {"*": "allow"}
    assert by_name["readonly"]["permission"]["*"] == "deny"


def test_default_permission_readonly_delivered(client: TestClient):
    _poll(client)
    client.patch(
        "/api/settings",
        json={"default_permission": permissions.dumps(permissions.READONLY)},
        headers=_admin_headers(),
    )
    poll = _poll(client)
    assert poll["config"]["permission"]["*"] == "deny"


def test_command_dispatch_denied_under_readonly(client: TestClient):
    _poll(client)
    client.patch(
        "/api/settings",
        json={"default_permission": permissions.dumps(permissions.READONLY)},
        headers=_admin_headers(),
    )
    resp = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE, "mode": "command", "instruction": "rm -rf /"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 403
    assert "denied" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_task_events_roundtrip(tmp_path):
    from agent_mesh.orchestrator.store.sqlite import SQLiteStore

    backend = SQLiteStore(str(tmp_path / "db.sqlite"))
    await backend.initialize()
    try:
        await backend.append_task_event(
            "t1", "dispatched", agent_id="node", user_id="u1", details={"mode": "command"}
        )
        await backend.append_task_event(
            "t1", "permission_denied", agent_id="node", details={"instruction": "rm -rf /"}
        )
        # Numeric agent refs must be coerced to text (PG TEXT columns reject ints).
        await backend.append_task_event("t2", "dispatched", agent_id=3, user_id=5)
        events = await backend.list_task_events("t1")
        assert [e["event_type"] for e in events] == ["dispatched", "permission_denied"]
        assert events[1]["details"]["instruction"] == "rm -rf /"
        assert events[0]["created_at"] is not None
        numeric = await backend.list_task_events("t2")
        assert numeric[0]["agent_id"] == "3"
        assert numeric[0]["user_id"] == "5"
    finally:
        await backend.close()


def test_template_permission_overrides_global(client: TestClient):
    _poll(client)
    created = client.post(
        "/api/templates",
        json={"name": "openbox", "permission": {"*": "allow"}},
        headers=_admin_headers(),
    )
    assert created.status_code == 200
    tpl_id = created.json()["template"]["id"]

    client.patch(
        f"/api/agents/{DEVICE}/template",
        json={"template_id": tpl_id},
        headers=_admin_headers(),
    )
    poll = _poll(client)
    assert poll["config"]["permission"] == {"*": "allow"}


def test_global_default_permission_setting(client: TestClient):
    _poll(client)
    resp = client.patch(
        "/api/settings",
        json={"default_permission": '{"*": "allow"}'},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    poll = _poll(client)
    assert poll["config"]["permission"] == {"*": "allow"}


def _make_user(client: TestClient, username: str = "viewer", ui: bool = False):
    """Create a non-admin user -> (user_id, headers)."""
    team_id = client.post(
        "/api/teams", json={"name": f"team-{username}"}, headers=_admin_headers()
    ).json()["team"]["team_id"]
    resp = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": "secret123",
            "role": "user",
            "team_id": team_id,
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    h = {"Authorization": f"Bearer {data['token']}"}
    if ui:
        h["X-Agent-Mesh-UI"] = "1"
    return data["user_id"], h


def test_non_admin_api_cannot_touch_templates(client: TestClient):
    # Without the Web-UI header, management endpoints are 404 for any user.
    _, h = _make_user(client)
    assert client.get("/api/templates", headers=h).status_code == 404
    assert client.post("/api/templates", json={"name": "x"}, headers=h).status_code == 404
    _poll(client)
    assert client.patch(
        f"/api/agents/{DEVICE}/template", json={"template_id": 1}, headers=h
    ).status_code == 404
    assert client.patch(
        "/api/settings", json={"llm_model": "m"}, headers=h
    ).status_code == 403


def test_non_admin_owns_only_own_templates(client: TestClient):
    global_tpl = client.post(
        "/api/templates", json={"name": "glob", "permission": {"*": "deny"}},
        headers=_admin_headers(),
    ).json()["template"]
    _, h = _make_user(client, ui=True)
    # An admin/global template is invisible and untouchable for a non-admin.
    listed = client.get("/api/templates", headers=h).json()["templates"]
    assert all(t["name"] != "glob" for t in listed)
    assert client.patch(
        f"/api/templates/{global_tpl['id']}", json={"name": "z"}, headers=h
    ).status_code == 404
    assert client.delete(
        f"/api/templates/{global_tpl['id']}", headers=h
    ).status_code == 404

    # A non-admin can create and manage their own template.
    mine = client.post(
        "/api/templates", json={"name": "mine", "permission": {"*": "allow"}}, headers=h
    )
    assert mine.status_code == 200
    tpl = mine.json()["template"]
    # Non-admins own what they create: by team (they are in one) or by user.
    assert tpl["owner_user_id"] is not None or tpl["owner_team_id"] is not None
    assert client.delete(
        f"/api/templates/{mine.json()['template']['id']}", headers=h
    ).status_code == 200


def test_non_admin_cannot_override_admin_template_binding(client: TestClient):
    _poll(client)
    # Admin binds a global (admin-owned) template -> the binding is locked.
    glob = client.post(
        "/api/templates", json={"name": "locked", "permission": {"*": "deny"}},
        headers=_admin_headers(),
    ).json()["template"]
    client.patch(
        f"/api/agents/{DEVICE}/template", json={"template_id": glob["id"]},
        headers=_admin_headers(),
    )
    uid, h = _make_user(client, ui=True)
    client.patch(
        f"/api/agents/{DEVICE}/access", json={"users": [uid]}, headers=_admin_headers()
    )
    mine = client.post(
        "/api/templates", json={"name": "mine2", "permission": {"*": "allow"}}, headers=h
    ).json()["template"]
    # Cannot override the admin's locked binding...
    assert client.patch(
        f"/api/agents/{DEVICE}/template", json={"template_id": mine["id"]}, headers=h
    ).status_code == 403
    # ...nor bind the admin's global template.
    assert client.patch(
        f"/api/agents/{DEVICE}/template", json={"template_id": glob["id"]}, headers=h
    ).status_code == 403
    assert client.patch(
        f"/api/agents/{DEVICE}/llm_config", json={"llm_model": "m"}, headers=h
    ).status_code == 403


def test_effective_description_node_over_template(client: TestClient):
    _poll(client)
    tpl = client.post(
        "/api/templates",
        json={
            "name": "desc-tpl",
            "description": "template note (说明)",
            "node_description": "template node desc (节点描述)",
        },
        headers=_admin_headers(),
    ).json()["template"]
    assert tpl["node_description"] == "template node desc (节点描述)"
    client.patch(
        f"/api/agents/{DEVICE}/template",
        json={"template_id": tpl["id"]},
        headers=_admin_headers(),
    )
    agent = client.get(f"/api/agents/{DEVICE}", headers=_admin_headers()).json()["agent"]
    assert agent["template_name"] == "desc-tpl"
    # The template's NODE description is used (not its human-facing 说明).
    assert agent["effective_description"] == "template node desc (节点描述)"

    # The node's own description wins over the template's.
    client.patch(
        f"/api/agents/{DEVICE}/description",
        json={"description": "node desc"},
        headers=_admin_headers(),
    )
    agent = client.get(f"/api/agents/{DEVICE}", headers=_admin_headers()).json()["agent"]
    assert agent["description"] == "node desc"
    assert agent["effective_description"] == "node desc"


def test_template_说明_does_not_leak_into_node_description(client: TestClient):
    # A template with only its own note (说明) must NOT provide a node description.
    _poll(client)
    tpl = client.post(
        "/api/templates",
        json={"name": "note-only", "description": "just a note"},
        headers=_admin_headers(),
    ).json()["template"]
    client.patch(
        f"/api/agents/{DEVICE}/template",
        json={"template_id": tpl["id"]},
        headers=_admin_headers(),
    )
    agent = client.get(f"/api/agents/{DEVICE}", headers=_admin_headers()).json()["agent"]
    assert agent["effective_description"] is None


def test_templates_hidden_from_api_without_ui_header(client: TestClient):
    # Management endpoints are not part of the API surface: even an admin token
    # without the Web-UI header gets 404, as if the route did not exist.
    h = {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}
    _poll(client)
    assert client.get("/api/templates", headers=h).status_code == 404
    assert client.post("/api/templates", json={"name": "x"}, headers=h).status_code == 404
    assert client.patch("/api/templates/1", json={"name": "y"}, headers=h).status_code == 404
    assert client.delete("/api/templates/1", headers=h).status_code == 404
    assert client.patch(
        f"/api/agents/{DEVICE}/template", json={"template_id": 1}, headers=h
    ).status_code == 404


def test_command_dispatch_has_no_allowed_tools_param(client: TestClient):
    _poll(client)
    # The dispatch surface no longer accepts allowed_tools; it is simply ignored
    # (permission comes from the node's template / global default).
    resp = client.post(
        "/api/tasks/dispatch",
        json={
            "agent_id": DEVICE,
            "mode": "command",
            "instruction": "echo hi",
            "allowed_tools": ["bash"],
        },
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
