import io
import tarfile

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from conftest import sqlite_client

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


@pytest_asyncio.fixture
async def client(tmp_path):
    db_path = tmp_path / "data" / "db.sqlite"
    async with sqlite_client(tmp_path, db_path=str(db_path), bootstrap=True) as c:
        yield c


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _edge_headers() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _poll(client, device_id="00:aa:bb:cc:dd:01", version=None, os="linux"):
    body = {
        "agent_id": "client",
        "device_id": device_id,
        "runtime": "opencode",
        "hostname": "host",
        "os": os,
        "arch": "x64",
    }
    if version:
        body["version"] = version
    return client.post(
        "/api/edge/poll_for_task", json=body, headers=_edge_headers()
    ).json()


# ----------------------------------------------------------------------
# LLM config sync
# ----------------------------------------------------------------------
def test_poll_carries_config_version_and_config(client: TestClient):
    resp = _poll(client)
    assert resp["config_version"] == "0"
    assert resp["config"]["llm_model"] == "anthropic/deepseek-v4-flash"
    assert resp["config"]["llm_api_key"] == ""
    assert "llm_base_url" in resp["config"]
    # The configurable model list reaches the edge (it builds the opencode map).
    assert "anthropic/deepseek-v4-flash" in resp["config"]["llm_models"]
    assert resp["config"]["system_prompt"] == ""


def test_settings_change_bumps_config_version(client: TestClient):
    resp = client.patch(
        "/api/settings",
        json={"llm_model": "vip/deepseek-v4-pro", "llm_base_url": "https://api.example.com/v1"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["config_version"] == "1"

    poll = _poll(client)
    assert poll["config_version"] == "1"
    assert poll["config"]["llm_model"] == "vip/deepseek-v4-pro"
    assert poll["config"]["llm_base_url"] == "https://api.example.com/v1"


def test_per_agent_llm_config_overrides_and_bumps(client: TestClient):
    _poll(client)
    resp = client.patch(
        "/api/agents/00:aa:bb:cc:dd:01/llm_config",
        json={"llm_model": "agent-specific-model"},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200

    # Config version must have been bumped so the node re-syncs.
    poll = _poll(client)
    assert poll["config_version"] == "1"
    assert poll["config"]["llm_model"] == "agent-specific-model"


def test_bootstrap_install_script_does_not_embed_llm_key(client: TestClient):
    # Configure a global LLM key; the generated install script must never
    # contain it (the key is delivered later via heartbeat config-sync).
    client.patch(
        "/api/settings",
        json={
            "llm_api_key": "sk-secret-key-123456",
            "llm_base_url": "https://gateway.example.com",
            "llm_model": "anthropic/deepseek-v4-flash",
        },
        headers=_admin_headers(),
    )
    resp = client.get("/api/bootstrap/install.sh", headers=_admin_headers())
    assert resp.status_code == 200
    assert "sk-secret-key-123456" not in resp.text
    assert "llm_api_key" not in resp.text.lower()
    assert "EDGE_LLM" not in resp.text


def test_bootstrap_install_script_detects_platform_client_side(client: TestClient):
    resp = client.get("/api/bootstrap/install.sh", headers=_admin_headers())
    assert resp.status_code == 200
    text = resp.text
    # OS/arch are resolved on the target, so the same command works everywhere.
    assert "uname -m" in text
    assert "agent-mesh-agent-${OS}-${ARCH}.tar.gz" in text
    # Actionable errors for missing prerequisites / failed download.
    assert "for c in curl tar" in text
    assert "is required but not installed" in text
    assert "failed to download the probe package" in text


# ----------------------------------------------------------------------
# Agent self-upgrade
# ----------------------------------------------------------------------
def test_upgrade_request_pushed_via_poll_then_cleared(client: TestClient):
    from agent_mesh.shared.constants import VERSION

    _poll(client, version="0.9.0")

    resp = client.post(
        "/api/agents/00:aa:bb:cc:dd:01/upgrade",
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["requested"] is True
    target = resp.json()["version"]
    assert target == VERSION

    # Next poll carries an upgrade directive while the node is still old.
    poll = _poll(client, version="0.9.0")
    assert poll["upgrade"]["version"] == target
    assert poll["upgrade"]["filename"] == "agent-mesh-agent-linux-x64.tar.gz"

    # The node reports it is already on the target version -> flag cleared.
    poll = _poll(client, version=target)
    assert "upgrade" not in poll

    resp = client.get("/api/agents/00:aa:bb:cc:dd:01", headers=_admin_headers())
    assert resp.json()["agent"]["upgrade_requested"] is False


def test_upgrade_duplicate_request_rejected(client: TestClient):
    _poll(client, version="0.9.0")
    client.post("/api/agents/00:aa:bb:cc:dd:01/upgrade", headers=_admin_headers())
    resp = client.post("/api/agents/00:aa:bb:cc:dd:01/upgrade", headers=_admin_headers())
    assert resp.status_code == 200
    assert resp.json()["requested"] is False


def test_upgrade_unknown_agent(client: TestClient):
    resp = client.post("/api/agents/9999/upgrade", headers=_admin_headers())
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# Auto-upgrade (stale nodes upgrade without a manual request)
# ----------------------------------------------------------------------
def test_auto_upgrade_pushed_without_manual_request(client: TestClient):
    from agent_mesh.shared.constants import VERSION

    poll = _poll(client, version="0.9.0")
    assert poll["upgrade"]["version"] == VERSION
    assert poll["upgrade"]["filename"] == "agent-mesh-agent-linux-x64.tar.gz"

    # Once the node reports the target version, no directive is sent.
    poll = _poll(client, version=VERSION)
    assert "upgrade" not in poll


def test_no_auto_upgrade_when_up_to_date(client: TestClient):
    from agent_mesh.shared.constants import VERSION

    poll = _poll(client, version=VERSION)
    assert "upgrade" not in poll


def test_auto_upgrade_can_be_disabled(client: TestClient):
    client.patch("/api/settings", json={"auto_upgrade": "0"}, headers=_admin_headers())
    poll = _poll(client, version="0.9.0")
    assert "upgrade" not in poll


@pytest_asyncio.fixture
async def win_client(tmp_path):
    """Client whose bootstrap dir also carries a Windows package."""
    db_path = tmp_path / "data" / "db.sqlite"
    async with sqlite_client(tmp_path, db_path=str(db_path), bootstrap=True) as c:
        (tmp_path / "data" / "bootstrap" / "agent-mesh-agent-win32-x64.tar.gz").write_bytes(b"pkg")
        yield c


def test_windows_node_receives_upgrade_directive(win_client: TestClient):
    from agent_mesh.shared.constants import VERSION

    poll = _poll(win_client, device_id="00:aa:bb:cc:dd:77", version="0.9.0", os="win32")
    assert poll["upgrade"]["version"] == VERSION
    assert poll["upgrade"]["filename"] == "agent-mesh-agent-win32-x64.tar.gz"

    # Up to date -> no directive (no Windows-specific skip, no loop).
    poll = _poll(win_client, device_id="00:aa:bb:cc:dd:77", version=VERSION, os="win32")
    assert "upgrade" not in poll


# ----------------------------------------------------------------------
# Node lifecycle is admin-only
# ----------------------------------------------------------------------
def _non_admin_token(client: TestClient, username: str) -> str:
    team = client.post(
        "/api/teams", json={"name": f"team-{username}"}, headers=_admin_headers()
    ).json()["team"]["team_id"]
    return client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": "secret123",
            "role": "user",
            "team_id": team,
        },
        headers=_admin_headers(),
    ).json()["token"]


def test_upgrade_and_delete_require_admin(client: TestClient):
    _poll(client, version="1.0.0")
    user_h = {"Authorization": f"Bearer {_non_admin_token(client, 'bob')}"}

    # A normal user cannot upgrade or delete a node.
    assert (
        client.post(
            "/api/agents/00:aa:bb:cc:dd:01/upgrade", headers=user_h
        ).status_code
        == 403
    )
    assert (
        client.delete("/api/agents/00:aa:bb:cc:dd:01", headers=user_h).status_code
        == 403
    )

    # Admin can (the fixture published a bootstrap package).
    assert (
        client.post(
            "/api/agents/00:aa:bb:cc:dd:01/upgrade", headers=_admin_headers()
        ).status_code
        == 200
    )
    assert (
        client.delete("/api/agents/00:aa:bb:cc:dd:01", headers=_admin_headers()).status_code
        == 200
    )


# ----------------------------------------------------------------------
# Probe package version + upload
# ----------------------------------------------------------------------
def _make_pkg(version: str, root: str = "agent-mesh-agent-linux-arm64") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in (
            (f"{root}/VERSION", version),
            (f"{root}/install.sh", "#!/bin/sh\necho hi\n"),
            (f"{root}/bin/agent-mesh-edge", "BIN"),
        ):
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_bootstrap_info_reports_versions(client: TestClient):
    from agent_mesh.shared.constants import SERVER_VERSION, VERSION

    data = client.get("/api/bootstrap/info", headers=_admin_headers()).json()
    assert data["server_version"] == SERVER_VERSION
    assert data["probe_version"] == VERSION

    # Non-admin cannot read the probe/version info.
    user_h = {"Authorization": f"Bearer {_non_admin_token(client, 'carol')}"}
    assert client.get("/api/bootstrap/info", headers=user_h).status_code == 403


def test_bootstrap_upload_publishes_package(client: TestClient):
    pkg = _make_pkg("9.9.9")
    resp = client.post(
        "/api/bootstrap",
        files={"file": ("agent-mesh-agent-linux-arm64.tar.gz", pkg, "application/gzip")},
        headers=_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["probe_version"] == "9.9.9"
    assert body["filename"] == "agent-mesh-agent-linux-arm64.tar.gz"

    info = client.get("/api/bootstrap/info", headers=_admin_headers()).json()
    assert info["probe_version"] == "9.9.9"
    assert any(
        p["filename"] == "agent-mesh-agent-linux-arm64.tar.gz"
        for p in info["packages"]
    )

    # Non-admin cannot publish a package.
    user_h = {"Authorization": f"Bearer {_non_admin_token(client, 'dave')}"}
    assert (
        client.post(
            "/api/bootstrap",
            files={"file": ("agent-mesh-agent-linux-arm64.tar.gz", pkg, "application/gzip")},
            headers=user_h,
        ).status_code
        == 403
    )

    # Garbage is rejected.
    assert (
        client.post(
            "/api/bootstrap",
            files={"file": ("bad.tar.gz", b"not a tarball", "application/gzip")},
            headers=_admin_headers(),
        ).status_code
        == 400
    )
