"""Bootstrap endpoints must not let request-controlled values inject script.

The installers are piped into `bash` / `iex`, so Host / mirror URLs / package
filenames are validated before they are embedded.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_mesh.orchestrator.api.bootstrap import (
    _safe_bootstrap_base,
    _valid_bootstrap_filename,
)

ADMIN_API_TOKEN = "admin-api-token-123"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def test_valid_bootstrap_filename():
    assert _valid_bootstrap_filename("agent-mesh-agent-linux-x64.tar.gz")
    assert _valid_bootstrap_filename("agent-mesh-agent-win32-arm64.tar.gz")
    assert _valid_bootstrap_filename("install.sh")
    assert _valid_bootstrap_filename("install.ps1")
    assert _valid_bootstrap_filename("VERSION")
    for bad in (
        "..",
        "../agent-mesh.db",
        "agent-mesh-agent-linux-x64",
        "agent-mesh-agent-windows-x64.tar.gz",
        "evil.sh",
        "install.sh.bak",
        "/etc/passwd",
    ):
        assert not _valid_bootstrap_filename(bad), bad


def test_safe_bootstrap_base():
    assert _safe_bootstrap_base("") == ""
    assert _safe_bootstrap_base(None) == ""
    assert _safe_bootstrap_base("https://mirror.example.com/probe") == (
        "https://mirror.example.com/probe"
    )
    for bad in (
        'https://x"; touch /tmp/pwn; #',
        "file:///etc/passwd",
        "https://x/$(id)",
        "https://x/`id`",
        "not a url",
    ):
        assert _safe_bootstrap_base(bad) == "", bad


def test_install_sh_rejects_malicious_host(client: TestClient):
    r = client.get(
        "/api/bootstrap/install.sh",
        headers={**_admin(), "Host": 'evil"; touch /tmp/pwn; #'},
    )
    assert r.status_code == 400
    assert "Host" in r.json()["detail"]


def test_install_ps1_rejects_malicious_host(client: TestClient):
    r = client.get(
        "/api/bootstrap/install.ps1",
        headers={**_admin(), "Host": "evil$(id)"},
    )
    assert r.status_code == 400


def test_install_sh_accepts_valid_host(client: TestClient):
    r = client.get("/api/bootstrap/install.sh", headers=_admin())
    assert r.status_code == 200
    assert 'BASE_URL="http://testserver"' in r.text
