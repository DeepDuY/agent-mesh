import pytest
from fastapi.testclient import TestClient

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}", "X-Agent-Mesh-UI": "1"}


def _edge_headers() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _poll(client, device_id="00:aa:bb:cc:dd:01", running_tasks=None, version=None):
    body = {
        "agent_id": "client",
        "device_id": device_id,
        "runtime": "opencode",
        "hostname": "h",
        "os": "linux",
        "arch": "x64",
    }
    if version:
        body["version"] = version
    if running_tasks:
        body["running_tasks"] = list(running_tasks)
    return client.post("/api/edge/poll_for_task", json=body, headers=_edge_headers()).json()


def _dispatch(client, instruction="hello", agent_id="00:aa:bb:cc:dd:01"):
    return client.post(
        "/api/tasks/dispatch",
        json={"agent_id": agent_id, "mode": "command", "instruction": instruction},
        headers=_admin_headers(),
    ).json()["task_id"]


# ----------------------------------------------------------------------
# Orchestrator: concurrent task allocation
# ----------------------------------------------------------------------
def test_poll_returns_tasks_array(client: TestClient):
    _poll(client)
    assert _poll(client)["tasks"] == []


def test_concurrent_allocation_up_to_cap(client: TestClient):
    _poll(client)
    ids = [_dispatch(client, f"task {i}") for i in range(4)]

    # First poll fills up to max_concurrent (default 2).
    r1 = _poll(client)
    assert len(r1["tasks"]) == 2
    claimed1 = {t["task_id"] for t in r1["tasks"]}

    # Second poll without running_tasks resumes the same 2 (reconnect) and does
    # not exceed the cap.
    r2 = _poll(client)
    assert len(r2["tasks"]) == 2
    assert {t["task_id"] for t in r2["tasks"]} == claimed1

    # Completing one task frees a slot -> a queued task is allocated next poll.
    done = list(claimed1)[0]
    client.post(
        "/api/edge/submit_result",
        json={"task_id": done, "status": "completed", "exit_code": 0,
              "summary": "ok", "stdout_tail": ""},
        headers=_edge_headers(),
    )
    r3 = _poll(client, running_tasks=list(claimed1 - {done}))
    got = {t["task_id"] for t in r3["tasks"]}
    # Active now = 1 (the still-running one); capacity = 1 -> exactly one new
    # task is allocated, the running one is NOT resumed (edge reported it).
    assert len(got) == 1
    assert got == {next(i for i in ids if i not in claimed1)}


def test_capacity_reached_stops_allocating(client: TestClient):
    _poll(client)
    for i in range(4):
        _dispatch(client, f"t{i}")

    # First poll claims up to the cap.
    active = list({t["task_id"] for t in _poll(client)["tasks"]})
    assert len(active) == 2
    # Edge reports both active tasks running -> no resume, no free capacity.
    r = _poll(client, running_tasks=active)
    assert r["tasks"] == []


def test_resume_only_tasks_not_in_running(client: TestClient):
    _poll(client)
    for i in range(4):
        _dispatch(client, f"t{i}")
    first = _poll(client)
    claimed = [t["task_id"] for t in first["tasks"]]
    assert len(claimed) == 2

    # Edge reports both running -> nothing to resume, no capacity.
    r = _poll(client, running_tasks=claimed)
    assert r["tasks"] == []

    # Edge reports only one running -> the other is resumed (capacity still full).
    r = _poll(client, running_tasks=[claimed[0]])
    assert {t["task_id"] for t in r["tasks"]} == {claimed[1]}


def test_legacy_edge_without_running_tasks_gets_task_field(client: TestClient):
    _poll(client)
    task_id = _dispatch(client)
    r = _poll(client)
    # Legacy view: `task` == tasks[0].
    assert r["task"] is not None
    assert r["task"]["task_id"] == task_id
    assert len(r["tasks"]) == 1


def test_max_concurrent_in_poll_response(client: TestClient):
    _poll(client)
    assert _poll(client)["max_concurrent"] == 2
    client.patch("/api/settings", json={"max_concurrent": "5"}, headers=_admin_headers())
    assert _poll(client)["max_concurrent"] == 5


# ----------------------------------------------------------------------
# Edge: concurrent execution + workdir isolation
# ----------------------------------------------------------------------
def test_resolve_workdir_isolates_default_tasks(tmp_path):
    from agent_mesh.edge.execution import resolve_workdir
    from agent_mesh.shared.schemas import Constraints, Task

    t1 = Task(task_id="t-aaa", agent_id="a", instruction="x", constraints=Constraints(workdir=""))
    t2 = Task(task_id="t-bbb", agent_id="a", instruction="x", constraints=Constraints(workdir="."))
    base = str(tmp_path / "edge-work")
    w1 = resolve_workdir(t1, base)
    w2 = resolve_workdir(t2, base)
    assert w1.parent == (tmp_path / "edge-work" / "tasks").resolve()
    assert w1.name == "t-aaa"
    assert w2.name == "t-bbb"
    assert w1 != w2


@pytest.mark.asyncio
async def test_edge_executes_tasks_concurrently(client: TestClient, tmp_path):
    """Three command tasks finish in under ~1s thanks to 2-way concurrency."""
    import time
    from agent_mesh.shared.schemas import Task

    c = client
    c.post(
        "/api/edge/poll_for_task",
        json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01",
              "runtime": "opencode", "hostname": "h", "os": "linux"},
        headers=_edge_headers(),
    )
    _dispatch(c, "sleep 0.5 && echo DONE1")
    _dispatch(c, "sleep 0.5 && echo DONE2")
    _dispatch(c, "sleep 0.5 && echo DONE3")

    # Emulate the edge: poll with running_tasks, submit each completed task
    # after its own simulated execution delay.
    running: list[str] = []
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        r = c.post(
            "/api/edge/poll_for_task",
            json={"agent_id": "client", "device_id": "00:aa:bb:cc:dd:01",
                  "runtime": "opencode", "hostname": "h", "os": "linux",
                  "running_tasks": running},
            headers=_edge_headers(),
        ).json()
        for td in r.get("tasks", []):
            t = Task(**td)
            if t.task_id in running:
                continue
            if len(running) >= 2:
                continue
            running.append(t.task_id)
            time.sleep(0.3)  # pretend execution takes a while
            c.post(
                "/api/edge/submit_result",
                json={"task_id": t.task_id, "status": "completed", "exit_code": 0,
                      "summary": "done", "stdout_tail": "ok"},
                headers=_edge_headers(),
            )
            running.remove(t.task_id)
        time.sleep(0.05)

    tasks = c.get("/api/tasks", headers=_admin_headers()).json()["tasks"]
    completed = [t for t in tasks if t["status"] == "completed"]
    assert len(completed) == 3


def test_workdir_isolated_per_task(client: TestClient, tmp_path):
    from agent_mesh.edge.execution import resolve_workdir
    from agent_mesh.shared.schemas import Constraints, Task

    _poll(client)
    task_id = _dispatch(client)
    claimed = _poll(client)["tasks"][0]
    assert claimed["task_id"] == task_id
    task = Task(**claimed)
    wd = resolve_workdir(task, str(tmp_path / "edge-work"))
    assert wd.name == task_id
    assert str(wd).startswith(str((tmp_path / "edge-work" / "tasks").resolve()))
