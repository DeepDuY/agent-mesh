"""Probe package distribution: external download base + GitHub release sync."""

from __future__ import annotations

ADMIN_API_TOKEN = "admin-api-token-123"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def test_install_scripts_default_to_server(client):
    sh = client.get("/api/bootstrap/install.sh", headers=_admin())
    assert sh.status_code == 200, sh.text
    assert 'BOOTSTRAP_BASE=""' in sh.text
    assert "/api/bootstrap/${PKG}" in sh.text

    ps = client.get("/api/bootstrap/install.ps1", headers=_admin())
    assert ps.status_code == 200, ps.text
    assert '$BootstrapBase = ""' in ps.text
    assert "/api/bootstrap/$pkg" in ps.text


def test_install_scripts_use_external_base(client):
    base = "https://github.com/DeepDuY/agent-mesh-edge/releases/latest/download"
    r = client.patch(
        "/api/settings", json={"bootstrap_download_base": base}, headers=_admin()
    )
    assert r.status_code == 200, r.text

    sh = client.get("/api/bootstrap/install.sh", headers=_admin()).text
    assert f'BOOTSTRAP_BASE="{base}"' in sh
    assert 'INSTALL_URL="${BOOTSTRAP_BASE%/}/${PKG}"' in sh

    ps = client.get("/api/bootstrap/install.ps1", headers=_admin()).text
    assert f'$BootstrapBase = "{base}"' in ps
    assert "$BootstrapBase" in ps and "TrimEnd" in ps


def test_sync_requires_repo(client):
    r = client.patch("/api/settings", json={"probe_release_repo": ""}, headers=_admin())
    assert r.status_code == 200, r.text
    r = client.post("/api/bootstrap/sync", headers=_admin())
    assert r.status_code == 400


def test_sync_uses_configured_repo(client, monkeypatch):
    from agent_mesh.orchestrator import probe_release

    captured: dict = {}

    async def fake_sync(repo, token, dest, **kwargs):
        captured["repo"] = repo
        captured["dest"] = str(dest)
        return {
            "repo": repo,
            "tag": "probe-v1.6.4",
            "version": "1.6.4",
            "synced": [{"filename": "agent-mesh-agent-linux-x64.tar.gz", "size": 1}],
        }

    monkeypatch.setattr(probe_release, "sync_release", fake_sync)
    r = client.patch(
        "/api/settings",
        json={"probe_release_repo": "DeepDuY/agent-mesh-edge"},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text

    r = client.post("/api/bootstrap/sync", headers=_admin())
    assert r.status_code == 200, r.text
    assert r.json()["tag"] == "probe-v1.6.4"
    assert captured["repo"] == "DeepDuY/agent-mesh-edge"
    assert captured["dest"].endswith("bootstrap")


def test_release_helpers():
    from agent_mesh.orchestrator.probe_release import _PKG_NAME_RE, _version_from_tag

    assert _version_from_tag("probe-v1.6.4") == "1.6.4"
    assert _version_from_tag("v9.1.0") == "9.1.0"
    assert _version_from_tag(None) is None
    assert _PKG_NAME_RE.match("agent-mesh-agent-win32-x64.tar.gz")
    assert _PKG_NAME_RE.match("agent-mesh-agent-darwin-arm64.tar.gz")
    assert not _PKG_NAME_RE.match("agent-mesh-agent-win32-x64.zip")
