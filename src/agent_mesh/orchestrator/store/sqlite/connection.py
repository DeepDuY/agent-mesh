from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.connection import (
    _dt_to_iso,
    _dump_json,
    _iso_to_dt,
    _load_json,
    SQLiteDatabase,
)

__all__ = [
    "SQLiteBase",
    "_dt_to_iso",
    "_iso_to_dt",
    "_load_json",
    "_dump_json",
]


class SQLiteBase:
    """Base class for SQLite store mixins.

    Owns a :class:`SQLiteDatabase` connection layer and exposes the same
    execution helpers the mixins call (``_execute`` / ``_execute_rowcount``),
    so store code only talks to the unified database connection layer.
    """

    def __init__(self, db_path: str = ""):
        self._db = SQLiteDatabase(db_path or ":memory:")

    async def initialize(self) -> None:
        await self._db.initialize()

    async def close(self) -> None:
        await self._db.close()

    async def _execute(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[Any]:
        return await self._db.execute(sql, params)

    async def _execute_rowcount(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> int:
        return await self._db.execute_rowcount(sql, params)
