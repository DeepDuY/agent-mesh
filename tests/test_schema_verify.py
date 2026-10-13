"""Tests for the startup DB schema self-check (store/connection/verify.py)."""

from __future__ import annotations

import pytest

from agent_mesh.orchestrator.store.connection.verify import verify_schema
from agent_mesh.orchestrator.store.sqlite import SQLiteStore

# Highest migration bundled with the code (store/migrations/025_task_reviews.sql).
_EXPECTED_MAX_VERSION = 25


@pytest.mark.asyncio
async def test_verify_schema_ok_after_migrations(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "agent-mesh.db"))
    await store.initialize()
    status = await verify_schema(store, "sqlite")
    await store.close()

    assert status["ok"] is True
    assert status["backend"] == "sqlite"
    assert status["schema_version"] == _EXPECTED_MAX_VERSION
    assert status["missing_tables"] == []
    assert status["missing_columns"] == {}
    assert status["pending_migrations"] == []


@pytest.mark.asyncio
async def test_verify_schema_detects_missing_table(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "agent-mesh.db"))
    await store.initialize()
    await store._execute("DROP TABLE task_reviews")
    status = await verify_schema(store, "sqlite")
    await store.close()

    assert status["ok"] is False
    assert "task_reviews" in status["missing_tables"]


@pytest.mark.asyncio
async def test_verify_schema_detects_pending_migration(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "agent-mesh.db"))
    await store.initialize()
    await store._execute(
        "DELETE FROM schema_migrations WHERE version = ?", (_EXPECTED_MAX_VERSION,)
    )
    status = await verify_schema(store, "sqlite")
    await store.close()

    assert status["ok"] is False
    assert status["pending_migrations"] == [_EXPECTED_MAX_VERSION]
