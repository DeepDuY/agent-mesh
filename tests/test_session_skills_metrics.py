import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from agent_mesh.orchestrator.store.sqlite import SQLiteStore
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import Constraints

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


def _make_skill_zip(name: str, description: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nInstructions.\n",
        )
    return buf.getvalue()


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _edge_headers() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _poll(client, device_id="00:aa:bb:cc:dd:01", metrics=None):
    body = {
        "agent_id": "node",
        "device_id": device_id,
        "runtime": "opencode",
        "hostname": "h",
        "os": "linux",
        "arch": "x64",
        "version": "1.3.0",
    }
    if metrics:
        body.update(metrics)
    return client.post(
        "/api/edge/poll_for_task", json=body, headers=_edge_headers()
    ).json()


# ----------------------------------------------------------------------
# LLM session id flow
# ----------------------------------------------------------------------
def test_dispatch_session_id_reuse_flow(client: TestClient):
    _poll(client)

    # First task: no session -> a fresh session is used and reported back.
    task_id = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": "00:aa:bb:cc:dd:01", "mode": "llm", "instruction": "task one"},
        headers=_admin_headers(),
    ).json()["task_id"]

    claimed = _poll(client)
    assert claimed["task"]["constraints"]["session_id"] is None

    client.post(
        "/api/edge/submit_result",
        json={
            "task_id": task_id,
            "status": "completed",
            "exit_code": 0,
            "summary": "done",
            "session_id": "ses_abc123",
        },
        headers=_edge_headers(),
    )

    detail = client.get(f"/api/tasks/{task_id}", headers=_admin_headers()).json()["task"]
    assert detail["result"]["session_id"] == "ses_abc123"

    # Second task reuses the previous session via the optional session_id field.
    task2 = client.post(
        "/api/tasks/dispatch",
        json={
            "agent_id": "00:aa:bb:cc:dd:01",
            "mode": "llm",
            "instruction": "task two",
            "session_id": "ses_abc123",
        },
        headers=_admin_headers(),
    ).json()["task_id"]

    claimed2 = _poll(client)
    assert claimed2["task"]["constraints"]["session_id"] == "ses_abc123"


# ----------------------------------------------------------------------
# Skills library
# ----------------------------------------------------------------------
def test_skill_upload_summary_download(client: TestClient):
    zip_bytes = _make_skill_zip("git-release", "Create consistent releases")
    resp = client.post(
        "/api/skills",
        files={"file": ("skill.zip", zip_bytes, "application/zip")},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["skill"] == {
        "name": "git-release",
        "description": "Create consistent releases",
        "version": 1,
        "enabled": True,
    }

    # Summary endpoint exposes metadata ONLY (no SKILL.md content).
    skills = client.get("/api/skills", headers=_edge_headers()).json()["skills"]
    assert skills == [{
        "name": "git-release",
        "description": "Create consistent releases",
        "version": 1,
        "enabled": True,
    }]
    raw = client.get("/api/skills", headers=_edge_headers()).text
    assert "Instructions" not in raw
    assert "git-release" in raw

    # The edge can download the full zip with its own (or global) token.
    resp = client.get("/api/skills/git-release/download", headers=_edge_headers())
    assert resp.status_code == 200
    assert resp.content == zip_bytes
    assert resp.headers.get("content-type") == "application/zip"

    # Re-upload bumps the version (edges can detect the change).
    resp = client.post(
        "/api/skills",
        files={"file": ("skill.zip", zip_bytes, "application/zip")},
        headers=_admin_headers(),
    )
    assert resp.json()["skill"]["version"] == 2

    # Disable / delete.
    resp = client.patch(
        "/api/skills/git-release",
        json={"enabled": False},
        headers=_admin_headers(),
    )
    assert resp.json()["skill"]["enabled"] is False
    resp = client.delete("/api/skills/git-release", headers=_admin_headers())
    assert resp.json()["deleted"] is True
    assert client.get("/api/skills", headers=_edge_headers()).json()["skills"] == []


def test_skill_upload_rejects_invalid_zip(client: TestClient):
    resp = client.post(
        "/api/skills",
        files={"file": ("bad.zip", b"not a zip", "application/zip")},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400

    # Valid zip but no frontmatter description.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "no frontmatter here\n")
    resp = client.post(
        "/api/skills",
        files={"file": ("bad.zip", buf.getvalue(), "application/zip")},
        headers=_admin_headers(),
    )
    assert resp.status_code == 400


def test_skill_requires_auth(client: TestClient):
    assert client.get("/api/skills").status_code == 401
    assert client.get("/api/skills/foo/download").status_code == 401


# ----------------------------------------------------------------------
# Resource metrics heartbeat
# ----------------------------------------------------------------------
def test_heartbeat_metrics_persisted_and_exposed(client: TestClient):
    _poll(client, metrics={
        "cpu_percent": 42.5,
        "mem_percent": 63.0,
        "mem_used_mb": 1024.0,
        "mem_total_mb": 8192.0,
    })
    agent = client.get(
        "/api/agents/00:aa:bb:cc:dd:01", headers=_admin_headers()
    ).json()["agent"]
    assert agent["cpu_percent"] == 42.5
    assert agent["mem_percent"] == 63.0
    assert agent["mem_used_mb"] == 1024.0
    assert agent["mem_total_mb"] == 8192.0
    assert agent["version"] == "1.3.0"


# ----------------------------------------------------------------------
# TaskStore unit: skills param stored on task constraints
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dispatch_skills_constraint_persists(tmp_path):
    backend = SQLiteStore(str(tmp_path / "agent-mesh.db"))
    await backend.initialize()
    try:
        store = TaskStore(store=backend)
        await store.heartbeat("node", "dev-1", "opencode", "h", telemetry={"os": "linux"})
        task_id = await store.dispatch(
            agent_id="dev-1",
            instruction="do it",
            mode="llm",
            constraints=Constraints(skills=["git-release"]),
        )
        task = await store.get_task(task_id)
        assert task.constraints.skills == ["git-release"]
        assert task.status == TaskStatus.QUEUED
    finally:
        await backend.close()
