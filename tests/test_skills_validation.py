"""Task/schedule skill validation: requested skills must exist and be enabled."""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
DEVICE = "00:aa:bb:cc:dd:01"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _edge() -> dict:
    return {"Authorization": "Bearer mcp-global-token"}


def _poll(client: TestClient) -> None:
    client.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": DEVICE, "runtime": "opencode",
              "hostname": "h", "os": "linux"},
        headers=_edge(),
    )


def _skill_zip(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n",
        )
    return buf.getvalue()


def _upload(client: TestClient, name: str) -> None:
    r = client.post(
        "/api/skills",
        files={"file": (f"{name}.zip", _skill_zip(name), "application/zip")},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text


def _dispatch(client: TestClient, skills: list[str]):
    return client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEVICE, "mode": "llm", "instruction": "hi", "skills": skills},
        headers=_admin(),
    )


def test_dispatch_accepts_known_enabled_skill(client: TestClient):
    _poll(client)
    _upload(client, "deploy-helper")
    assert _dispatch(client, ["deploy-helper"]).status_code == 200


def test_dispatch_rejects_unknown_skill(client: TestClient):
    _poll(client)
    r = _dispatch(client, ["does-not-exist"])
    assert r.status_code == 400
    assert "skill not found" in r.json()["detail"]


def test_dispatch_rejects_disabled_skill(client: TestClient):
    _poll(client)
    _upload(client, "flaky")
    client.patch("/api/skills/flaky", json={"enabled": False}, headers=_admin())
    r = _dispatch(client, ["flaky"])
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"]


def test_schedule_rejects_unknown_skill(client: TestClient):
    _poll(client)
    r = client.post(
        "/api/schedules",
        json={"name": "s1", "cron": "0 3 * * *", "agent_id": DEVICE,
              "mode": "llm", "instruction": "hi", "skills": ["nope"]},
        headers=_admin(),
    )
    assert r.status_code == 400
    assert "skill not found" in r.json()["detail"]
