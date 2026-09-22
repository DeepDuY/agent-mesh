"""Windows bootstrap support: package naming, PowerShell installer, upload."""

from __future__ import annotations

import io
import json
import tarfile

from agent_mesh.orchestrator.api.bootstrap import _PKG_NAME_RE

ADMIN_API_TOKEN = "admin-api-token-123"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _make_package(version: str = "1.6.4") -> bytes:
    """A minimal agent-mesh-agent-win32-x64.tar.gz package."""
    root = "agent-mesh-agent-win32-x64"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = {
            f"{root}/VERSION": version.encode(),
            f"{root}/install.ps1": b"# installer",
            f"{root}/MANIFEST.json": json.dumps({"version": version, "files": {}}).encode(),
            f"{root}/bin/agent-mesh-edge.exe": b"BINARY",
            f"{root}/bin/opencode.exe": b"OPENCODE",
        }
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_pkg_name_regex_accepts_win32():
    for name in (
        "agent-mesh-agent-win32-x64",
        "agent-mesh-agent-win32-arm64",
        "agent-mesh-agent-linux-x64",
        "agent-mesh-agent-darwin-arm64",
    ):
        assert _PKG_NAME_RE.match(name), name
    for bad in (
        "agent-mesh-agent-windows-x64",
        "agent-mesh-agent-win32-ia64",
        "agent-mesh-agent-win32",
    ):
        assert not _PKG_NAME_RE.match(bad), bad


def test_install_ps1_script(client):
    r = client.get("/api/bootstrap/install.ps1", headers=_admin())
    assert r.status_code == 200, r.text
    body = r.text
    assert "__BASE_URL__" not in body
    assert "agent-mesh-agent-win32-" in body
    assert "install.ps1" in body
    assert "http://testserver" in body
    # Re-running the bootstrap must be an idempotent (re)install, otherwise an
    # existing install aborts before the Scheduled Task gets (re)registered.
    assert "-Force" in body


def test_upload_and_download_win32_package(client):
    pkg = _make_package("1.6.4")
    r = client.post(
        "/api/bootstrap",
        files={"file": ("agent-mesh-agent-win32-x64.tar.gz", pkg, "application/gzip")},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["filename"] == "agent-mesh-agent-win32-x64.tar.gz"
    assert data["probe_version"] == "1.6.4"

    info = client.get("/api/bootstrap/info", headers=_admin())
    assert info.status_code == 200
    names = [p["filename"] for p in info.json()["packages"]]
    assert "agent-mesh-agent-win32-x64.tar.gz" in names

    dl = client.get(
        "/api/bootstrap/agent-mesh-agent-win32-x64.tar.gz", headers=_admin()
    )
    assert dl.status_code == 200
    assert dl.content == pkg


def test_upload_rejects_unknown_platform(client):
    root = "agent-mesh-agent-windows-x64"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"1.6.4"
        info = tarfile.TarInfo(f"{root}/VERSION")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    r = client.post(
        "/api/bootstrap",
        files={"file": ("pkg.tar.gz", buf.getvalue(), "application/gzip")},
        headers=_admin(),
    )
    assert r.status_code == 400
    assert "unexpected package name" in r.json()["detail"]
