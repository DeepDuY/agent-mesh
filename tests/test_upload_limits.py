"""Configurable upload limits: file library, probe artifacts, global quota.

Limits live in the `settings` table (seeded by migration 020) and are read per
request, so a config-page change applies immediately.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEV1 = "00:aa:bb:cc:dd:01"
MB = 1024 * 1024


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin() -> dict:
    return _bearer(ADMIN_API_TOKEN)


def _global() -> dict:
    return _bearer(GLOBAL_TOKEN)


def _settings(client: TestClient, **kw) -> dict:
    r = client.patch(
        "/api/settings",
        json={k: str(v) for k, v in kw.items()},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    return r.json()["settings"]


def _poll(client: TestClient, device_id: str = DEV1, agent_id: str = "node") -> dict:
    r = client.post(
        "/api/edge/poll_for_task",
        json={
            "agent_id": agent_id,
            "device_id": device_id,
            "runtime": "opencode",
            "hostname": agent_id,
            "os": "linux",
            "arch": "x64",
            "version": "1.0.0",
        },
        headers=_global(),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _dispatch(client: TestClient) -> str:
    _poll(client)
    r = client.post(
        "/api/tasks/dispatch",
        json={"agent_id": DEV1, "mode": "command", "instruction": "echo hi"},
        headers=_admin(),
    )
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _upload(client: TestClient, task_id: str, name: str, size: int):
    return client.post(
        f"/api/artifacts/{task_id}",
        files={"files": (name, b"x" * size, "application/octet-stream")},
        headers=_global(),
    )


def test_default_limits_seeded_and_exposed(client: TestClient):
    s = client.get("/api/settings", headers=_admin()).json()["settings"]
    assert s["file_max_size_mb"] == "100"
    assert s["artifact_max_size_mb"] == "100"
    assert s["artifact_task_total_mb"] == "100"
    assert s["artifact_total_mb"] == "200"
    assert s["artifact_evict_oldest"] == "1"
    assert s["artifact_timeout_s"] == "300"


def test_file_upload_limit_is_configurable(client: TestClient):
    _settings(client, file_max_size_mb=1)
    ok = client.post(
        "/api/files",
        files={"files": ("a.bin", b"x" * MB, "application/octet-stream")},
        headers=_admin(),
    )
    assert ok.status_code == 200, ok.text
    over = client.post(
        "/api/files",
        files={"files": ("b.bin", b"x" * (MB + 1), "application/octet-stream")},
        headers=_admin(),
    )
    assert over.status_code == 413
    assert "exceeds max size" in over.json()["detail"]


def test_artifact_single_file_and_task_total(client: TestClient):
    task = _dispatch(client)
    _settings(
        client,
        artifact_max_size_mb=1,
        artifact_task_total_mb=1,
        artifact_total_mb=100,
    )

    over = _upload(client, task, "big.bin", MB + 1)
    assert over.status_code == 413
    assert "exceeds max size" in over.json()["detail"]

    assert _upload(client, task, "a.bin", 600 * 1024).status_code == 200
    two = _upload(client, task, "b.bin", 600 * 1024)
    assert two.status_code == 413
    assert "artifact total" in two.json()["detail"]


def test_global_quota_evicts_oldest(client: TestClient):
    t1 = _dispatch(client)
    t2 = _dispatch(client)
    _settings(
        client,
        artifact_max_size_mb=1,
        artifact_task_total_mb=10,
        artifact_total_mb=1,
        artifact_evict_oldest=1,
    )

    first = _upload(client, t1, "old.bin", 600 * 1024)
    assert first.status_code == 200, first.text
    old_aid = first.json()["artifacts"][0]["artifact_id"]

    second = _upload(client, t2, "new.bin", 600 * 1024)
    assert second.status_code == 200, second.text
    new_aid = second.json()["artifacts"][0]["artifact_id"]

    # Oldest artifact evicted; newest kept and downloadable.
    assert client.get(f"/api/artifacts/{t1}/{old_aid}", headers=_admin()).status_code == 404
    assert client.get(f"/api/artifacts/{t2}/{new_aid}", headers=_admin()).status_code == 200


def test_global_quota_rejects_when_eviction_disabled(client: TestClient):
    t1 = _dispatch(client)
    _settings(
        client,
        artifact_max_size_mb=1,
        artifact_task_total_mb=10,
        artifact_total_mb=1,
        artifact_evict_oldest=0,
    )
    assert _upload(client, t1, "a.bin", 600 * 1024).status_code == 200
    over = _upload(client, t1, "b.bin", 600 * 1024)
    assert over.status_code == 413
    assert "quota exceeded" in over.json()["detail"]


def test_patch_settings_rejects_invalid_limits(client: TestClient):
    cases = (
        ("file_max_size_mb", 0),
        ("artifact_max_size_mb", -5),
        ("artifact_total_mb", "abc"),
        ("artifact_timeout_s", 10**9),
    )
    for key, value in cases:
        r = client.patch("/api/settings", json={key: value}, headers=_admin())
        assert r.status_code == 400, (key, r.text)


def test_timeout_change_bumps_config_version_and_syncs(client: TestClient):
    before = client.get("/api/settings", headers=_admin()).json()["settings"]["config_version"]
    _settings(client, artifact_timeout_s=600)
    after = client.get("/api/settings", headers=_admin()).json()["settings"]["config_version"]
    assert int(after) > int(before)

    body = _poll(client)
    assert body["config"]["artifact_timeout_s"] == "600"


def test_limits_are_admin_only(client: TestClient):
    assert client.get("/api/settings", headers=_global()).status_code in (401, 403)
    r = client.patch(
        "/api/settings", json={"file_max_size_mb": 1}, headers=_global()
    )
    assert r.status_code in (401, 403)
