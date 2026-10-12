"""AI review gate: policy matching, scoring, hold/deny/approve flows."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from agent_mesh.orchestrator import review as review_mod

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE_A = "00:aa:bb:cc:dd:01"
DEVICE_B = "00:aa:bb:cc:dd:02"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _poll(client: TestClient, device: str, agent_id: str = "node") -> dict:
    return client.post(
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
    ).json()


def _set_policy(client: TestClient, **kw) -> None:
    policy = {
        "enabled": True,
        "threshold": 60,
        "human_review": True,
        "bypass_admin": False,
        "expire_s": 1800,
        **kw,
    }
    r = client.patch(
        "/api/settings", json={"review_policy": json.dumps(policy)}, headers=_admin()
    )
    assert r.status_code == 200, r.text


def _seed_review_gateway(client: TestClient) -> None:
    r = client.patch(
        "/api/settings",
        json={"llm_base_url": "http://gateway.example/v1"},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text


def _fake_reviewer(monkeypatch, score: int, reason: str = "r", violations=None):
    async def fake(**kwargs):
        return json.dumps(
            {"score": score, "reason": reason, "violations": violations or []}
        )

    monkeypatch.setattr(review_mod, "_call_reviewer", fake)


def test_api_style_resolution_and_model_prefix():
    p = review_mod.load_policy(json.dumps({"enabled": True}))
    assert review_mod._resolve_api_style(
        p, "https://gw.example/api/anthropic", "anthropic/deepseek-v4-flash"
    ) == "anthropic"
    assert review_mod._resolve_api_style(
        p, "https://gw.example/v1", "gpt-4o"
    ) == "openai"
    forced = review_mod.load_policy(json.dumps({"enabled": True, "api_style": "openai"}))
    assert review_mod._resolve_api_style(
        forced, "https://gw.example/api/anthropic", "anthropic/x"
    ) == "openai"
    assert review_mod._strip_provider("anthropic/deepseek-v4-flash") == "deepseek-v4-flash"
    assert review_mod._strip_provider("vip/kimi-k2.7-code") == "vip/kimi-k2.7-code"


# ----------------------------------------------------------------------
# Pure policy helpers
# ----------------------------------------------------------------------
def test_load_policy_defaults_and_parse():
    p = review_mod.load_policy("")
    assert p.enabled is False and p.threshold == 60 and p.human_review is True
    p = review_mod.load_policy(json.dumps({"enabled": True, "threshold": 200, "agents": ["x"]}))
    assert p.enabled is True
    assert p.threshold == 100  # clamped
    assert p.agents == ["x"]


def test_scope_matching():
    class A:
        id = 1
        agent_id = "nodeA"
        device_id = DEVICE_A
        alias = "prod-db"

    policy = review_mod.load_policy(json.dumps({"enabled": True}))
    # Empty selectors = review everything.
    assert review_mod.scope_matches(policy, agent=A(), team_ids=[], user={"role": "user"})

    only_b = review_mod.load_policy(json.dumps({"enabled": True, "agents": [DEVICE_B]}))
    assert not review_mod.scope_matches(
        only_b, agent=A(), team_ids=[], user={"role": "user"}
    )
    by_alias = review_mod.load_policy(json.dumps({"enabled": True, "agents": ["prod-db"]}))
    assert review_mod.scope_matches(
        by_alias, agent=A(), team_ids=[], user={"role": "user"}
    )
    by_team = review_mod.load_policy(json.dumps({"enabled": True, "teams": ["t1"]}))
    assert review_mod.scope_matches(
        by_team, agent=A(), team_ids=["t1"], user={"role": "user"}
    )
    by_user = review_mod.load_policy(json.dumps({"enabled": True, "users": ["u1"]}))
    assert review_mod.scope_matches(
        by_user, agent=A(), team_ids=[], user={"user_id": "u1", "role": "user"}
    )
    # Admin bypass.
    assert not review_mod.scope_matches(
        policy, agent=A(), team_ids=[], user={"role": "admin"}
    )
    no_bypass = review_mod.load_policy(
        json.dumps({"enabled": True, "bypass_admin": False})
    )
    assert review_mod.scope_matches(
        no_bypass, agent=A(), team_ids=[], user={"role": "admin"}
    )


# ----------------------------------------------------------------------
# Dispatch integration
# ----------------------------------------------------------------------
def test_low_score_holds_for_human_then_approve(client: TestClient, monkeypatch):
    _poll(client, DEVICE_A)
    _seed_review_gateway(client)
    _set_policy(client)
    _fake_reviewer(monkeypatch, 10, "looks dangerous", ["rm -rf /"])

    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE_A, "mode": "command", "instruction": "rm -rf /"},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_review"
    task_id = body["task_id"]

    pending = client.get(
        "/api/tasks?status=pending_review", headers=_admin()
    ).json()["tasks"]
    held = [t for t in pending if t["task_id"] == task_id]
    assert held and held[0]["review_score"] == 10
    assert held[0]["review_reason"] == "looks dangerous"

    ok = client.post(f"/api/tasks/{task_id}/approve", headers=_admin())
    assert ok.status_code == 200, ok.text
    task = client.get(f"/api/tasks/{task_id}", headers=_admin()).json()["task"]
    assert task["status"] == "queued"

    # The node can now claim it.
    claimed = _poll(client, DEVICE_A)
    assert claimed["tasks"][0]["task_id"] == task_id


def test_low_score_without_human_review_is_denied(client: TestClient, monkeypatch):
    _poll(client, DEVICE_A)
    _seed_review_gateway(client)
    _set_policy(client, human_review=False)
    _fake_reviewer(monkeypatch, 5, "forbidden")

    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE_A, "mode": "llm", "instruction": "wipe the disk"},
        headers=_admin(),
    )
    assert r.status_code == 403, r.text

    denied = client.get("/api/tasks?status=denied", headers=_admin()).json()["tasks"]
    assert any(t["review_score"] == 5 for t in denied)


def test_high_score_dispatches_normally(client: TestClient, monkeypatch):
    _poll(client, DEVICE_A)
    _seed_review_gateway(client)
    _set_policy(client)
    _fake_reviewer(monkeypatch, 95, "safe read-only")

    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE_A, "mode": "command", "instruction": "ls -la"},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert r.json()["review_score"] == 95

    hist = client.get("/api/reviews", headers=_admin()).json()["reviews"]
    assert any(h["action"] == "allowed" and h["score"] == 95 for h in hist)


def test_reviewer_failure_fails_closed(client: TestClient, monkeypatch):
    _poll(client, DEVICE_A)
    _seed_review_gateway(client)
    _set_policy(client, human_review=False)

    async def boom(**kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(review_mod, "_call_reviewer", boom)
    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE_A, "mode": "command", "instruction": "ls"},
        headers=_admin(),
    )
    assert r.status_code == 403, r.text


def test_scope_limits_which_nodes_are_reviewed(client: TestClient, monkeypatch):
    _poll(client, DEVICE_A, "nodeA")
    _poll(client, DEVICE_B, "nodeB")
    _seed_review_gateway(client)
    _set_policy(client, agents=[DEVICE_B])
    _fake_reviewer(monkeypatch, 1, "no")

    # DEVICE_A is out of scope -> dispatched normally, no review.
    a = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE_A, "mode": "command", "instruction": "ls"},
        headers=_admin(),
    )
    assert a.status_code == 200 and a.json()["status"] == "queued"

    # DEVICE_B is in scope -> reviewed and held (low score).
    b = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE_B, "mode": "command", "instruction": "ls"},
        headers=_admin(),
    )
    assert b.status_code == 200 and b.json()["status"] == "pending_review"


def test_disabled_policy_skips_review(client: TestClient, monkeypatch):
    _poll(client, DEVICE_A)
    _set_policy(client, enabled=False)

    async def boom(**kwargs):
        raise AssertionError("reviewer must not be called when disabled")

    monkeypatch.setattr(review_mod, "_call_reviewer", boom)
    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE_A, "mode": "command", "instruction": "ls"},
        headers=_admin(),
    )
    assert r.status_code == 200 and r.json()["status"] == "queued"


def test_numeric_agent_id_is_recorded(client: TestClient, monkeypatch):
    """The main agent may pass the numeric node id; the audit must coerce it."""
    _poll(client, DEVICE_A)
    nodes = client.get("/api/agents", headers=_admin()).json()["agents"]
    node_id = next(a["id"] for a in nodes if a["device_id"] == DEVICE_A)
    _seed_review_gateway(client)
    _set_policy(client)
    _fake_reviewer(monkeypatch, 90, "ok")

    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": node_id, "mode": "command", "instruction": "ls"},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    reviews = client.get("/api/reviews", headers=_admin()).json()["reviews"]
    assert any(x["score"] == 90 and isinstance(x["agent_id"], str) for x in reviews)


def test_reviews_search_and_pagination(client: TestClient, monkeypatch):
    """History supports fuzzy search + paging and reports a total."""
    _poll(client, DEVICE_A)
    _seed_review_gateway(client)
    _set_policy(client)
    _fake_reviewer(monkeypatch, 90, "only-readable-ok")

    for _ in range(3):
        client.post(
            "/api/tasks/dispatch",
            json={"agent_id": DEVICE_A, "mode": "command", "instruction": "ls"},
            headers=_admin(),
        )

    page = client.get(
        "/api/reviews?search=only-readable&limit=2&offset=0", headers=_admin()
    ).json()
    assert page["total"] == 3
    assert len(page["reviews"]) == 2
    page2 = client.get(
        "/api/reviews?search=only-readable&limit=2&offset=2", headers=_admin()
    ).json()
    assert len(page2["reviews"]) == 1

    none = client.get(
        "/api/reviews?search=no-such-thing", headers=_admin()
    ).json()
    assert none["total"] == 0 and none["reviews"] == []


def test_review_result_is_cached(client: TestClient, monkeypatch):
    """Repeating the same dispatch must not re-hit the reviewer."""
    _poll(client, DEVICE_A)
    _seed_review_gateway(client)
    _set_policy(client)
    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        return json.dumps({"score": 95, "reason": "ok"})

    monkeypatch.setattr(review_mod, "_call_reviewer", fake)
    for _ in range(3):
        r = client.post(
            "/api/tasks/dispatch",
            json={"agent_id": DEVICE_A, "mode": "command", "instruction": "echo cached"},
            headers=_admin(),
        )
        assert r.status_code == 200 and r.json()["status"] == "queued"
    assert calls["n"] == 1


def test_batch_review_holds_low_scores(client: TestClient, monkeypatch):
    _poll(client, DEVICE_A, "nodeA")
    _poll(client, DEVICE_B, "nodeB")
    _seed_review_gateway(client)
    _set_policy(client)
    _fake_reviewer(monkeypatch, 10, "danger")

    r = client.post(
        "/api/tasks/dispatch-batch",
        json={
            "targets": [DEVICE_A, DEVICE_B],
            "mode": "command",
            "instruction": "rm -rf /tmp/x",
        },
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["queued"] == 0
    assert len(data["pending_review"]) == 2
    assert data["denied"] == []


def test_reviews_endpoint_is_admin_only(client: TestClient):
    # Create a regular user.
    team_id = client.post(
        "/api/teams", json={"name": "t1"}, headers=_admin()
    ).json()["team"]["team_id"]
    data = client.post(
        "/api/auth/users",
        json={"username": "u1", "password": "secret123", "role": "user", "team_id": team_id},
        headers=_admin(),
    ).json()
    h_user = {"Authorization": f"Bearer {data['token']}"}
    assert client.get("/api/reviews", headers=h_user).status_code == 403
    assert client.get("/api/reviews", headers=_admin()).status_code == 200


def _admin() -> dict:
    # The admin API token is an admin user; the UI header unlocks settings.
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}
