from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Shared value helpers
# ---------------------------------------------------------------------------
def _dt_to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _iso_to_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _dump_json(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _utcnow() -> datetime:
    """Naive UTC now, matching PG TIMESTAMP (without time zone) columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pg_param(value: Any) -> Any:
    """Normalise a bind parameter for PG: aware datetimes -> naive (UTC).

    PG ``TIMESTAMP`` (without time zone) columns reject offset-aware datetimes;
    naive values (with implicit UTC semantics) are accepted.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class Database:
    """Unified database connection layer.

    Centralises connection lifecycle (creation, pooling, per-process isolation)
    and the execution primitives used by the store layer. Concrete backends
    (``SQLiteDatabase`` / ``PostgresDatabase``) implement the same primitives so
    store code only ever talks to this interface.

    All SQL uses ``?`` placeholders; the Postgres backend rewrites them to
    ``$1/$2/...`` internally.
    """

    async def initialize(self) -> None:
        """Create tables / run migrations / seed defaults."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def execute(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[Any]:
        """Run a statement; returns dict-like rows (empty for writes)."""
        raise NotImplementedError

    async def execute_rowcount(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> int:
        """Run a write statement; returns affected row count."""
        raise NotImplementedError

    async def fetchrow(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> Any | None:
        """Run a SELECT returning a single dict-like row or None."""
        raise NotImplementedError

    async def executemany(
        self, sql: str, params_list: list[tuple[Any, ...]]
    ) -> int:
        """Run a write statement over many parameter tuples; returns count."""
        raise NotImplementedError

    async def dequeue(self, agent_id: str) -> str | None:
        """Atomically take the oldest queued task for ``agent_id`` or None."""
        raise NotImplementedError
