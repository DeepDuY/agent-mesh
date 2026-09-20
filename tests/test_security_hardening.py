"""§6/§7/§8 hardening.

- §6: `/api/settings` is restricted to the bundled Web UI and returns a
  whitelist (never `session_secret`).
- §7: non-admins never see per-node LLM credentials.
- §8: an edge node's own token may only download files attached to its tasks.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_mesh.orchestrator.api.agent_common import dump_agent
from agent_mesh.shared.schemas import AgentStatus

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE = "00:aa:bb:cc:dd:01"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _admin_no_ui() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


# ---------------------------------------------------------------------------
# §6 settings scope + whitelist
# ---------------------------------------------------------------------------
def test_settings_requires_ui_header(client: TestClient):
    assert client.get("/api/settings", headers=_admin_no_ui()).status_code == 404
    assert (
        client.patch("/api/settings", json={"auto_upgrade": "1"}, headers=_admin_no_ui()).status_code
        == 404
    )


def test_settings_response_is_whitelisted(client: TestClient):
    r = client.get("/api/settings", headers=_admin())
    assert r.status_code == 200
    settings = r.json()["settings"]
    assert "session_secret" not in settings
    assert settings.get("agent_mesh_token") == GLOBAL_TOKEN
    # A few keys the config page relies on must survive the whitelist.
    assert "public_url" in settings
    assert "config_version" in settings


# ---------------------------------------------------------------------------
# §7 agent detail credential redaction
# ---------------------------------------------------------------------------
def _agent_with_secrets() -> AgentStatus:
    return AgentStatus(
        id=1,
        agent_id="a",
        device_id="d",
        llm_api_key="sk-secret",
        llm_base_url="http://llm.internal",
        llm_model="m",
        access={"users": ["u-1"]},
    )


class _NonAdmin:
    def is_admin(self, user) -> bool:
        return False


class _Admin:
    def is_admin(self, user) -> bool:
        return True


def test_dump_agent_hides_credentials_for_non_admin():
    data = dump_agent(_NonAdmin(), _agent_with_secrets(), {"role": "user"})
    for key in ("llm_api_key", "llm_base_url", "llm_model", "access"):
        assert key not in data


def test_dump_agent_keeps_credentials_for_admin():
    data = dump_agent(_Admin(), _agent_with_secrets(), {"role": "admin"})
    assert data["llm_api_key"] == "sk-secret"
    assert data["access"] == {"users": ["u-1"]}


# ---------------------------------------------------------------------------
# §8 agent token file access
# ---------------------------------------------------------------------------
def _register(client: TestClient) -> str:
    return client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "node",
            "device_id": DEVICE,
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
        },
        headers=_admin(),
    ).json()["agent_token"]


def _upload(client: TestClient, name: str, content: bytes) -> dict:
    return client.post(
        "/api/files",
        files=[("files", (name, content, "text/plain"))],
        headers=_admin(),
    ).json()["files"][0]


def test_agent_token_only_downloads_its_task_attachments(client: TestClient):
    agent_token = _register(client)
    attached = _upload(client, "attached.txt", b"attached-data")
    unrelated = _upload(client, "secret.txt", b"secret-data")

    dispatch = client.post(
        "/api/tasks/dispatch",
        headers=_admin(),
        json={
            "agent_id": DEVICE,
            "mode": "command",
            "instruction": "cat attached.txt",
            "attachments": [attached["file_id"]],
        },
    )
    assert dispatch.status_code == 200

    agent_headers = {"Authorization": f"Bearer {agent_token}"}
    assert (
        client.get(f"/api/files/{attached['file_id']}", headers=agent_headers).status_code == 200
    )
    assert (
        client.get(f"/api/files/{unrelated['file_id']}", headers=agent_headers).status_code == 404
    )
    # User and global callers are unaffected.
    assert client.get(f"/api/files/{unrelated['file_id']}", headers=_admin()).status_code == 200
    assert (
        client.get(
            f"/api/files/{unrelated['file_id']}",
            headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
        ).status_code
        == 200
    )
