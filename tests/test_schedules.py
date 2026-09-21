"""Scheduled / recurring tasks: API CRUD, ownership, and the tick dispatcher."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"
DEVICE = "00:aa:bb:cc:dd:02"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _edge() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _poll(client: TestClient) -> dict:
    body = {
        "agent_id": "client",
        "device_id": DEVICE,
        "runtime": "opencode",
        "hostname": "h",
        "os": "linux",
    }
    return client.post("/api/edge/poll_for_task", json=body, headers=_edge()).json()


def _create(client: TestClient, **overrides) -> dict:
    body = {
        "name": "nightly",
        "cron": "0 3 * * *",
        "agent_id": DEVICE,
        "mode": "command",
        "instruction": "echo hi",
    }
    body.update(overrides)
    return client.post("/api/schedules", json=body, headers=_admin())


def test_schedule_crud(client: TestClient):
    _poll(client)
    r = _create(client)
    assert r.status_code == 200, r.text
    schedule = r.json()["schedule"]
    assert schedule["name"] == "nightly"
    assert schedule["next_run_at"] is not None
    assert schedule["enabled"] is True
    sid = schedule["id"]

    listed = client.get("/api/schedules", headers=_admin()).json()["schedules"]
    assert [s["id"] for s in listed] == [sid]

    got = client.get(f"/api/schedules/{sid}", headers=_admin()).json()["schedule"]
    assert got["cron"] == "0 3 * * *"

    patched = client.patch(
        f"/api/schedules/{sid}",
        json={"cron": "*/30 * * * *", "enabled": False},
        headers=_admin(),
    ).json()["schedule"]
    assert patched["cron"] == "*/30 * * * *"
    assert patched["enabled"] is False

    assert client.delete(f"/api/schedules/{sid}", headers=_admin()).status_code == 200
    assert client.get(f"/api/schedules/{sid}", headers=_admin()).status_code == 404


def test_schedule_validation(client: TestClient):
    _poll(client)
    assert _create(client, cron="not a cron").status_code == 400
    assert _create(client, agent_id="nope").status_code == 404
    assert _create(client).status_code == 200
    assert _create(client).status_code == 409  # duplicate name


def test_schedule_run_now(client: TestClient):
    _poll(client)
    sid = _create(client).json()["schedule"]["id"]
    r = client.post(f"/api/schedules/{sid}/run", headers=_admin())
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    task = client.get(f"/api/tasks/{task_id}", headers=_admin()).json()["task"]
    assert task["status"] == "queued"
    schedule = client.get(f"/api/schedules/{sid}", headers=_admin()).json()["schedule"]
    assert schedule["last_task_id"] == task_id


def test_schedule_visibility(client: TestClient):
    _poll(client)
    _create(client)  # owned by admin (no team)
    # A non-admin user must not see or fetch the admin's schedule.
    team_id = client.post(
        "/api/teams", json={"name": "t1"}, headers=_admin()
    ).json()["team"]["team_id"]
    created = client.post(
        "/api/auth/users",
        json={"username": "bob", "password": "secret123", "role": "user", "team_id": team_id},
        headers=_admin(),
    ).json()
    bob_token = created["token"]
    bob = {"Authorization": f"Bearer {bob_token}"}
    assert client.get("/api/schedules", headers=bob).json()["schedules"] == []
    sid = client.get("/api/schedules", headers=_admin()).json()["schedules"][0]["id"]
    assert client.get(f"/api/schedules/{sid}", headers=bob).status_code == 404


@pytest.mark.asyncio
async def test_tick_dispatches_advances_and_skips_overlap(tmp_path):
    from agent_mesh.orchestrator.store.sqlite import SQLiteStore
    from agent_mesh.orchestrator.task_store import TaskStore
    from agent_mesh.shared.constants import TaskStatus

    backend = SQLiteStore(str(tmp_path / "schedules.db"))
    await backend.initialize()
    await backend.set_setting("default_permission", '{"*": "allow"}')
    try:
        store = TaskStore(store=backend)
        await store.heartbeat("client", DEVICE, "opencode", "h", telemetry={"os": "linux"})
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        sid = await backend.create_schedule(
            name="s1",
            cron="* * * * *",
            agent_ref=DEVICE,
            instruction="echo hi",
            mode="command",
            next_run_at=past,
        )

        await store.tick_schedules()
        schedule = await backend.get_schedule(sid)
        assert schedule["last_status"] == "queued"
        assert schedule["last_task_id"]
        tasks = await store.list_tasks(agent_id=DEVICE)
        assert [t.task_id for t in tasks] == [schedule["last_task_id"]]
        assert tasks[0].status == TaskStatus.QUEUED
        # next_run_at advanced into the future (cadence anchored on the due time).
        assert schedule["next_run_at"] > datetime.now(timezone.utc)

        # Previous task is still active -> next due run is skipped, not queued.
        await backend.update_schedule(sid, next_run_at=past)
        await store.tick_schedules()
        again = await backend.get_schedule(sid)
        assert again["last_status"] == "skipped"
        assert len(await store.list_tasks(agent_id=DEVICE)) == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_disabled_schedule_not_run(tmp_path):
    from agent_mesh.orchestrator.store.sqlite import SQLiteStore
    from agent_mesh.orchestrator.task_store import TaskStore

    backend = SQLiteStore(str(tmp_path / "disabled.db"))
    await backend.initialize()
    try:
        store = TaskStore(store=backend)
        await store.heartbeat("client", DEVICE, "opencode", "h", telemetry={"os": "linux"})
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        await backend.create_schedule(
            name="off",
            cron="* * * * *",
            agent_ref=DEVICE,
            instruction="echo hi",
            mode="command",
            enabled=False,
            next_run_at=past,
        )
        await store.tick_schedules()
        assert await store.list_tasks(agent_id=DEVICE) == []
    finally:
        await backend.close()
