import hashlib
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from agent_mesh.orchestrator.task_store import TaskStore

ADMIN_API_TOKEN = "admin-api-token-123"


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _global_headers() -> dict:
    return {"Authorization": "Bearer mcp-global-token"}


def _register(client: TestClient) -> None:
    client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "client",
            "device_id": "00:aa:bb:cc:dd:01",
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
        },
        headers=_global_headers(),
    )


def test_upload_list_download_delete(client: TestClient):
    content = b"hello agent-mesh"
    md5 = hashlib.md5(content).hexdigest()

    resp = client.post(
        "/api/files",
        files=[("files", ("hello.txt", content, "text/plain"))],
        headers=_admin_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()["files"][0]
    assert data["filename"] == "hello.txt"
    assert data["md5"] == md5
    assert data["download_url"] == f"/api/files/{data['file_id']}"
    file_id = data["file_id"]

    # Dedup: identical md5 + filename returns the existing file_id.
    resp = client.post(
        "/api/files",
        files=[("files", ("hello.txt", content, "text/plain"))],
        headers=_admin_headers(),
    )
    assert resp.json()["files"][0]["file_id"] == file_id

    # Multiple files in one request.
    resp = client.post(
        "/api/files",
        files=[
            ("files", ("a.txt", b"aaa", "text/plain")),
            ("files", ("b.txt", b"bbb", "text/plain")),
        ],
        headers=_admin_headers(),
    )
    assert len(resp.json()["files"]) == 2

    # List.
    resp = client.get("/api/files", headers=_admin_headers())
    assert len(resp.json()["files"]) == 3

    # Download with the edge's global token; md5 header must match.
    resp = client.get(f"/api/files/{file_id}", headers=_global_headers())
    assert resp.status_code == 200
    assert resp.content == content
    assert resp.headers.get("x-file-md5") == md5

    # Download with a user token too.
    resp = client.get(f"/api/files/{file_id}", headers=_admin_headers())
    assert resp.status_code == 200

    # Missing auth.
    assert client.get("/api/files").status_code == 401

    # Delete.
    resp = client.delete(f"/api/files/{file_id}", headers=_admin_headers())
    assert resp.json()["deleted"] is True
    assert client.get(f"/api/files/{file_id}", headers=_admin_headers()).status_code == 404
    assert client.delete(f"/api/files/{file_id}", headers=_admin_headers()).status_code == 404


def test_dispatch_with_attachments(client: TestClient):
    content = b"payload-data"
    md5 = hashlib.md5(content).hexdigest()
    resp = client.post(
        "/api/files",
        files=[("files", ("data.bin", content, "application/octet-stream"))],
        headers=_admin_headers(),
    )
    file_id = resp.json()["files"][0]["file_id"]

    _register(client)
    resp = client.post(
        "/api/tasks/dispatch",
        headers=_admin_headers(),
        json={
            "agent_id": "00:aa:bb:cc:dd:01",
            "mode": "command",
            "instruction": "echo hi",
            "attachments": [file_id],
        },
    )
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    task = client.get(f"/api/tasks/{task_id}", headers=_admin_headers()).json()["task"]
    assert len(task["attachments"]) == 1
    att = task["attachments"][0]
    assert att["file_id"] == file_id
    assert att["filename"] == "data.bin"
    assert att["md5"] == md5
    assert att["download_url"] == f"/api/files/{file_id}"

    # The edge claim response carries the same attachments.
    claimed = client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": "client",
            "device_id": "00:aa:bb:cc:dd:01",
            "runtime": "opencode",
            "hostname": "h",
            "os": "linux",
        },
        headers=_global_headers(),
    ).json()["task"]
    assert claimed["attachments"][0]["file_id"] == file_id


def test_dispatch_missing_attachment_rejected(client: TestClient):
    _register(client)
    resp = client.post(
        "/api/tasks/dispatch",
        headers=_admin_headers(),
        json={
            "agent_id": "00:aa:bb:cc:dd:01",
            "mode": "command",
            "instruction": "echo hi",
            "attachments": ["f-nope"],
        },
    )
    assert resp.status_code == 400


def test_dispatch_without_attachments_still_works(client: TestClient):
    _register(client)
    resp = client.post(
        "/api/tasks/dispatch",
        headers=_admin_headers(),
        json={
            "agent_id": "00:aa:bb:cc:dd:01",
            "mode": "command",
            "instruction": "echo hi",
        },
    )
    assert resp.status_code == 200
    task = client.get(
        f"/api/tasks/{resp.json()['task_id']}", headers=_admin_headers()
    ).json()["task"]
    assert task["attachments"] == []


def test_skill_pack_download(client: TestClient):
    headers = _admin_headers()
    resp = client.get("/api/skill-pack/agent-mesh", headers=headers)
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("application/zip")

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert "agent-mesh/SKILL.md" in names
    assert "agent-mesh/.env" in names
    assert any(n.startswith("agent-mesh/references/") for n in names)

    # The token lives ONLY in .env, never in the Markdown.
    env = zf.read("agent-mesh/.env").decode()
    assert "AGENT_MESH_TOKEN=admin-api-token-123" in env
    skill_md = zf.read("agent-mesh/SKILL.md").decode()
    assert "admin-api-token-123" not in skill_md
    # No public_url configured -> an explicit "ask the user" note is injected.
    assert "未配置" in skill_md

    # Configure public_url: placeholders are filled and .env carries the base URL.
    client.patch(
        "/api/settings",
        json={"public_url": "http://10.0.0.1:8000"},
        headers=headers,
    )
    resp = client.get("/api/skill-pack/agent-mesh", headers=headers)
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    skill_md = zf.read("agent-mesh/SKILL.md").decode()
    assert "http://10.0.0.1:8000" in skill_md
    assert "<orchestrator-host>" not in skill_md
    env = zf.read("agent-mesh/.env").decode()
    assert "AGENT_MESH_BASE_URL=http://10.0.0.1:8000/api" in env
    # references are personalized too.
    nodes = zf.read("agent-mesh/references/nodes.md").decode()
    assert "<orchestrator-host>" not in nodes


def test_skill_pack_requires_user_token(client: TestClient):
    # The global token is not a user credential -> must be rejected.
    resp = client.get("/api/skill-pack/agent-mesh", headers=_global_headers())
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# SQLite round-trip: attachments persist through create_task/get_task.
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sqlite_attachments_roundtrip(tmp_path):
    from agent_mesh.orchestrator.store.sqlite import SQLiteStore
    from agent_mesh.shared.schemas import FileRef

    backend = SQLiteStore(str(tmp_path / "agent-mesh.db"))
    await backend.initialize()
    store = TaskStore(store=backend)
    await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "h", telemetry={"os": "linux"})
    refs = [
        FileRef(
            file_id="f-abc123",
            filename="payload.bin",
            size=3,
            content_type="application/octet-stream",
            md5="abc",
            download_url="/api/files/f-abc123",
        )
    ]
    task_id = await store.dispatch(
        agent_id="00:aa:bb:cc:dd:01",
        instruction="hello",
        attachments=refs,
    )
    task = await store.get_task(task_id)
    assert task is not None
    assert len(task.attachments) == 1
    assert task.attachments[0].file_id == "f-abc123"
    assert task.attachments[0].md5 == "abc"
    assert task.attachments[0].download_url == "/api/files/f-abc123"
    await backend.close()
