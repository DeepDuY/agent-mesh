import asyncio
import subprocess
import tarfile

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from agent_mesh.edge.agent import EdgeAgent
from agent_mesh.edge.config_writer import WRAPPER_SCRIPT, apply_llm_config
from conftest import sqlite_client

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


@pytest_asyncio.fixture
async def client(tmp_path):
    db_path = tmp_path / "data" / "db.sqlite"
    async with sqlite_client(tmp_path, db_path=str(db_path), bootstrap=True) as c:
        yield c


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


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


# ----------------------------------------------------------------------
# Edge config persistence (unit)
# ----------------------------------------------------------------------
def test_apply_llm_config_updates_edge_env(tmp_path):
    install = tmp_path / "agent"
    etc = install / "etc"
    etc.mkdir(parents=True)
    (etc / "edge.env").write_text(
        "EDGE_AGENT_ID=node\nEDGE_LLM_API_KEY=old-key\nEDGE_LLM_MODEL=old-model\nLOG_LEVEL=INFO\n",
        encoding="utf-8",
    )
    apply_llm_config(
        str(install),
        {"llm_api_key": "new-key", "llm_base_url": "https://new.example.com", "llm_model": "new-model"},
        "3",
    )
    env = (etc / "edge.env").read_text(encoding="utf-8")
    assert "EDGE_LLM_API_KEY=new-key" in env
    assert "EDGE_LLM_BASE_URL=https://new.example.com" in env
    assert "EDGE_LLM_MODEL=new-model" in env
    assert "EDGE_AGENT_ID=node" in env
    assert "LOG_LEVEL=INFO" in env
    assert (etc / "config_version").read_text(encoding="utf-8") == "3"


def test_apply_llm_config_appends_missing_keys(tmp_path):
    install = tmp_path / "agent"
    (install / "etc").mkdir(parents=True)
    (install / "etc" / "edge.env").write_text("EDGE_AGENT_ID=node\n", encoding="utf-8")
    apply_llm_config(
        str(install),
        {"llm_api_key": "k", "llm_base_url": "", "llm_model": "m"},
        "1",
    )
    env = (install / "etc" / "edge.env").read_text(encoding="utf-8")
    assert "EDGE_LLM_API_KEY=k" in env
    assert "EDGE_LLM_MODEL=m" in env


# ----------------------------------------------------------------------
# Edge self-upgrade execution (unit)
# ----------------------------------------------------------------------
def _make_package(install: object, version: str = "2.0.0") -> bytes:
    """Build an in-memory tar.gz mimicking the bootstrap package layout."""
    import io

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def _dir(name):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tar.addfile(info, None)

        def _file(name, data):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        _dir("agent-mesh-agent-linux-x64/")
        _dir("agent-mesh-agent-linux-x64/bin/")
        _file("agent-mesh-agent-linux-x64/bin/agent-mesh-edge", b"#!/bin/sh\necho NEW-BIN\n")
        _file("agent-mesh-agent-linux-x64/bin/opencode", b"#!/bin/sh\necho NEW-OPENCODE\n")
        _file("agent-mesh-agent-linux-x64/VERSION", version.encode())
    return buf.getvalue()


def _make_installed_layout(tmp_path):
    install = tmp_path / "opt" / "agent-mesh-agent"
    (install / "bin").mkdir(parents=True)
    (install / "etc").mkdir(parents=True)
    (install / "bin" / "agent-mesh-edge.bin").write_text("OLDBIN", encoding="utf-8")
    (install / "bin" / "opencode").write_text("OLDOPENCODE", encoding="utf-8")
    (install / "bin" / "agent-mesh-edge").write_text(
        WRAPPER_SCRIPT, encoding="utf-8",
    )
    (install / "etc" / "edge.env").write_text("EDGE_AGENT_ID=node\n", encoding="utf-8")
    (install / "etc" / "agent_version").write_text("1.0.0", encoding="utf-8")
    return install


def test_perform_upgrade_replaces_binary_and_marks_upgrade(tmp_path, monkeypatch):
    install = _make_installed_layout(tmp_path)
    agent = EdgeAgent(
        agent_id="node",
        orchestrator_url="http://127.0.0.1:8000",
        token="tok",
        runtime="opencode",
        workdir=".",
        llm_api_key="",
        llm_base_url="",
        llm_model="m",
        install_dir=str(install),
    )

    package_bytes = _make_package(install)
    async def fake_download(url, dest):
        open(dest, "wb").write(package_bytes)

    monkeypatch.setattr(agent.client, "download_to", fake_download)
    execv_calls = []
    monkeypatch.setattr("os.execv", lambda path, argv: execv_calls.append((path, argv)))

    asyncio.run(agent._perform_upgrade({"version": "2.0.0", "filename": "pkg.tar.gz"}))

    assert (install / "bin" / "agent-mesh-edge.bin").read_text() == "#!/bin/sh\necho NEW-BIN\n"
    assert (install / "bin" / "agent-mesh-edge.bin.old").read_text() == "OLDBIN"
    assert (install / "bin" / "opencode").read_text() == "#!/bin/sh\necho NEW-OPENCODE\n"
    assert (install / "etc" / "agent_version").read_text() == "2.0.0"
    assert (install / "etc" / "agent_version.bak").read_text() == "1.0.0"
    assert (install / "etc" / "upgrading").exists()
    wrapper = (install / "bin" / "agent-mesh-edge").read_text()
    assert "rolled back" in wrapper
    assert execv_calls == [(str(install / "bin" / "agent-mesh-edge"),
                            [str(install / "bin" / "agent-mesh-edge")])]
    # staging dir cleaned up
    assert not (install / "etc" / "upgrade").exists()


def test_perform_upgrade_skips_non_installed_layout(tmp_path, monkeypatch):
    # No wrapper / .bin layout (dev mode) -> upgrade must be skipped, not crash.
    install = tmp_path / "not-installed"
    install.mkdir(parents=True)
    agent = EdgeAgent(
        agent_id="node",
        orchestrator_url="http://127.0.0.1:8000",
        token="tok",
        runtime="opencode",
        workdir=".",
        llm_api_key="",
        llm_base_url="",
        llm_model="m",
        install_dir=str(install),
    )
    execv_calls = []
    monkeypatch.setattr("os.execv", lambda path, argv: execv_calls.append((path, argv)))

    async def fake_download(url, dest):
        raise AssertionError("should not download in dev mode")

    monkeypatch.setattr(agent.client, "download_to", fake_download)
    asyncio.run(agent._perform_upgrade({"version": "2.0.0"}))
    assert execv_calls == []


def test_wrapper_first_boot_runs_new_then_crash_rolls_back(tmp_path):
    install = _make_installed_layout(tmp_path)
    (install / "etc" / "agent_version").write_text("1.0.0", encoding="utf-8")
    (install / "etc" / "agent_version.bak").write_text("0.9.0", encoding="utf-8")
    # Post-upgrade state: new binary staged but never confirmed healthy.
    (install / "etc" / "upgrading").touch()
    (install / "bin" / "agent-mesh-edge.bin").write_text("NEWBIN", encoding="utf-8")
    (install / "bin" / "agent-mesh-edge.bin.old").write_text("OLDBIN", encoding="utf-8")

    # First boot after upgrade: wrapper runs the new binary (no rollback yet).
    wrapper = install / "bin" / "agent-mesh-edge"
    script = wrapper.read_text().replace('exec "$BIN" "$@"', "echo STARTED-WITH:$BIN")
    wrapper.write_text(script, encoding="utf-8")
    out = subprocess.run(
        ["bash", str(wrapper)], capture_output=True, text=True
    )
    assert out.stdout == "STARTED-WITH:%s\n" % (install / "bin" / "agent-mesh-edge.bin")
    assert (install / "etc" / "upgrade-started").exists()
    assert (install / "bin" / "agent-mesh-edge.bin").read_text() == "NEWBIN"

    # Second boot (the new binary crashed before confirming health): roll back.
    out = subprocess.run(
        ["bash", str(wrapper)], capture_output=True, text=True
    )
    assert "rolled back" in out.stderr
    assert (install / "bin" / "agent-mesh-edge.bin").read_text() == "OLDBIN"
    assert not (install / "etc" / "upgrading").exists()
    assert not (install / "etc" / "upgrade-started").exists()
    # Version manifest restored to the previous version.
    assert (install / "etc" / "agent_version").read_text() == "0.9.0"

    # Healthy confirmation (from the edge agent) clears marker, backup, version.bak.
    (install / "etc" / "upgrading").touch()
    (install / "etc" / "upgrade-started").touch()
    (install / "bin" / "agent-mesh-edge.bin.old").write_text("x", encoding="utf-8")
    (install / "etc" / "agent_version.bak").write_text("y", encoding="utf-8")
    agent = EdgeAgent(
        agent_id="node",
        orchestrator_url="http://127.0.0.1:8000",
        token="tok",
        runtime="opencode",
        workdir=".",
        llm_api_key="",
        llm_base_url="",
        llm_model="m",
        install_dir=str(install),
    )
    agent._confirm_upgrade_healthy()
    assert not (install / "etc" / "upgrading").exists()
    assert not (install / "etc" / "upgrade-started").exists()
    assert not (install / "bin" / "agent-mesh-edge.bin.old").exists()
    assert not (install / "etc" / "agent_version.bak").exists()
