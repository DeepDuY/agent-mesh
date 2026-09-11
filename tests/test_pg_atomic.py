"""Regression test: PG `update_task_status(expected_status=...)` must bind the
conditional status into the WHERE clause (not the SET list) and keep the param
order intact. A previous implementation appended `expected_status` to the SET
columns while params carried `[..., task_id, expected_status]`, so SQLite's
atomic timeout sweep silently no-oped on PostgreSQL.
"""

from datetime import datetime, timezone

import pytest

from agent_mesh.orchestrator.store.pg import PostgresStore


class _FakeDb:
    def __init__(self) -> None:
        self.captured: list[tuple[str, tuple]] = []

    async def execute_rowcount(self, sql: str, params: tuple | None = None) -> int:
        self.captured.append((sql, tuple(params) if params else ()))
        return 1

    async def execute(self, sql: str, params: tuple | None = None) -> list:
        return []

    async def fetchrow(self, sql: str, params: tuple | None = None):
        return None


@pytest.mark.asyncio
async def test_pg_update_task_status_expected_status_where_binding() -> None:
    fake = _FakeDb()
    store = PostgresStore(dsn="postgresql://fake/fake")
    store._db = fake  # type: ignore[assignment]

    await store.update_task_status(
        "t-abc123",
        "timed_out",
        finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        expected_status="working",
    )

    assert fake.captured, "expected one UPDATE statement"
    sql, params = fake.captured[0]

    # The conditional status must be a WHERE predicate, not another SET column.
    assert sql.count("status = ?") == 2
    assert "WHERE task_id = ?" in sql
    assert "AND status = ?" in sql

    # Param order: [status, ...fields, task_id, expected_status].
    assert params[0] == "timed_out"
    assert params[-2] == "t-abc123"
    assert params[-1] == "working"


@pytest.mark.asyncio
async def test_pg_update_task_status_no_expected_status() -> None:
    fake = _FakeDb()
    store = PostgresStore(dsn="postgresql://fake/fake")
    store._db = fake  # type: ignore[assignment]

    await store.update_task_status("t-abc123", "failed")

    sql, params = fake.captured[0]
    assert sql.count("status = ?") == 1
    assert "AND status = ?" not in sql
    assert params == ("failed", "t-abc123")
