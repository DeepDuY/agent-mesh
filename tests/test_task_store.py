import asyncio
import pytest
import pytest_asyncio

from agent_mesh.orchestrator.store.sqlite import SQLiteStore
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import Constraints, TaskResult


@pytest_asyncio.fixture
async def store(tmp_path):
    backend = SQLiteStore(str(tmp_path / "agent-mesh.db"))
    await backend.initialize()
    s = TaskStore(store=backend, sweep_interval_s=0.1, offline_after_s=0.2)
    await s.start_sweepers()
    yield s
    await s.stop_sweepers()
    await backend.close()


@pytest.mark.asyncio
async def test_dispatch_and_claim(store: TaskStore):
    await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    task_id = await store.dispatch(
        agent_id="00:aa:bb:cc:dd:01",
        instruction="hello",
        constraints=Constraints(),
    )
    tasks, key = await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    assert key == "00:aa:bb:cc:dd:01"
    assert len(tasks) == 1
    assert tasks[0].task_id == task_id
    # The returned Task snapshot predates the DB status flip; re-fetch to assert
    # the persisted claim (the SQLite backend returns a detached object).
    assert (await store.get_task(task_id)).status == TaskStatus.ASSIGNED


@pytest.mark.asyncio
async def test_submit_result(store: TaskStore):
    await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    task_id = await store.dispatch(
        agent_id="00:aa:bb:cc:dd:01",
        instruction="hello",
    )
    tasks, _ = await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    await store.mark_started(tasks[0].task_id)
    result = TaskResult(
        status="completed",
        exit_code=0,
        stdout_tail="ok",
        stderr_tail="",
        duration_ms=100,
        summary="done",
    )
    accepted = await store.submit_result(tasks[0].task_id, result)
    assert accepted
    task = await store.get_task(tasks[0].task_id)
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_offline_returns_to_queue(store: TaskStore):
    await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    task_id = await store.dispatch(
        agent_id="00:aa:bb:cc:dd:01",
        instruction="hello",
    )
    await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    tasks, _ = await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    assert tasks[0].task_id == task_id
    assert (await store.get_task(task_id)).status == TaskStatus.ASSIGNED
    # Wait for offline sweeper to return task to queue.
    await asyncio.sleep(0.4)
    task = await store.get_task(task_id)
    assert task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_cancel_queued_task(store: TaskStore):
    await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    task_id = await store.dispatch(
        agent_id="00:aa:bb:cc:dd:01",
        instruction="hello",
    )
    accepted = await store.cancel_task(task_id)
    assert accepted
    task = await store.get_task(task_id)
    assert task.status == TaskStatus.CANCELLED
    # A queued cancelled task must not be claimable.
    claimed, _ = await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    assert claimed == []
    # Cancelling again is a no-op.
    assert await store.cancel_task(task_id) is False


@pytest.mark.asyncio
async def test_cancel_working_task(store: TaskStore):
    await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    task_id = await store.dispatch(
        agent_id="00:aa:bb:cc:dd:01",
        instruction="hello",
    )
    tasks, _ = await store.heartbeat("client", "00:aa:bb:cc:dd:01", "opencode", "host", telemetry={"os": "linux"})
    assert tasks[0].task_id == task_id
    await store.mark_started(task_id)
    accepted = await store.cancel_task(task_id)
    assert accepted
    task = await store.get_task(task_id)
    assert task.status == TaskStatus.CANCELLED
    # Submit after cancel must be rejected.
    result = TaskResult(
        status="completed",
        exit_code=0,
        stdout_tail="late",
        stderr_tail="",
        duration_ms=10,
        summary="done",
    )
    assert await store.submit_result(task_id, result) is False
