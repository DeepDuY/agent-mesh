from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.connection import pg_schema, pg_schema_upgrade
from agent_mesh.orchestrator.store.connection.base import Database, _pg_param


def _rewrite_placeholders(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``$1/$2/...`` (Postgres style).

    Scans outside single-quoted string literals so ``?`` characters inside
    strings are untouched. Used by :class:`PostgresDatabase`.
    """
    out: list[str] = []
    n = 0
    i = 0
    in_str = False
    while i < len(sql):
        c = sql[i]
        if in_str:
            out.append(c)
            if c == "'":
                # Handle escaped quotes ('' is a literal quote in SQL).
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "?":
            n += 1
            out.append(f"${n}")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


class PostgresDatabase(Database):
    """PostgreSQL connection layer: per-process lazy asyncpg pool.

    Each OS process (main + forked uvicorn workers) gets its own pool, created
    lazily on first use. This sidesteps asyncpg's event-loop binding and the
    fork-unsafety of sharing a pool across processes.
    """

    def __init__(
        self,
        *,
        dsn: str = "",
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "agent_mesh",
        password: str = "",
        database: str = "agent_mesh",
        min_size: int = 2,
        max_size: int = 10,
    ):
        self._dsn = dsn or f"postgresql://{user}:{password}@{host}:{port}/{database}"
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None
        self._pool_pid: int | None = None

    async def initialize(self) -> None:
        import asyncpg

        # One-off connection (not pooled) to build the schema. The pool is
        # created lazily on first _acquire(), bound to the caller's loop.
        conn = await asyncpg.connect(self._dsn)
        try:
            await pg_schema.create_schema(conn)
            await pg_schema_upgrade.ensure_user_schema_upgrade(conn)
            await pg_schema_upgrade.ensure_agent_schema_upgrade(conn)
            await pg_schema_upgrade.ensure_task_schema_upgrade(conn)
            await pg_schema.ensure_session_secret(conn)
            await pg_schema.ensure_settings(conn)
            await pg_schema.ensure_default_templates(conn)
            await pg_schema.ensure_admin_user(conn)
        finally:
            await conn.close()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._pool_pid = None

    async def _acquire(self):
        """Return a pooled connection, creating/rebuilding the pool for this PID."""
        import os

        import asyncpg

        pid = os.getpid()
        if self._pool is None or self._pool_pid != pid:
            if self._pool is not None:
                # Forked process inherited a pool bound to the parent's loop.
                # Drop it (do not close: the parent still uses it); a fresh pool
                # is created below bound to this process's loop.
                self._pool = None
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
            )
            self._pool_pid = pid
        return await self._pool.acquire()

    async def _release(self, conn: Any) -> None:
        try:
            await self._pool.release(conn)
        except Exception:
            pass

    async def execute(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[Any]:
        pg_sql = _rewrite_placeholders(sql)
        pg_params = tuple(_pg_param(p) for p in (params or ()))
        conn = await self._acquire()
        try:
            rows = await conn.fetch(pg_sql, *pg_params)
            return rows
        finally:
            await self._release(conn)

    async def execute_rowcount(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> int:
        import re

        pg_sql = _rewrite_placeholders(sql)
        pg_params = tuple(_pg_param(p) for p in (params or ()))
        conn = await self._acquire()
        try:
            result = await conn.execute(pg_sql, *pg_params)
            m = re.search(r"(\d+)$", result)
            return int(m.group(1)) if m else 0
        finally:
            await self._release(conn)

    async def fetchrow(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> Any | None:
        pg_sql = _rewrite_placeholders(sql)
        pg_params = tuple(_pg_param(p) for p in (params or ()))
        conn = await self._acquire()
        try:
            return await conn.fetchrow(pg_sql, *pg_params)
        finally:
            await self._release(conn)

    async def executemany(
        self, sql: str, params_list: list[tuple[Any, ...]]
    ) -> int:
        if not params_list:
            return 0
        pg_sql = _rewrite_placeholders(sql)
        pg_params_list = [
            tuple(_pg_param(p) for p in row) for row in params_list
        ]
        conn = await self._acquire()
        try:
            await conn.executemany(pg_sql, pg_params_list)
            return len(params_list)
        finally:
            await self._release(conn)

    async def dequeue(self, agent_id: str) -> str | None:
        row = await self.fetchrow(
            """
            DELETE FROM task_queue
            WHERE task_id = (
                SELECT task_id FROM task_queue
                WHERE agent_id = ?
                ORDER BY enqueued_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING task_id
            """,
            (agent_id,),
        )
        return row["task_id"] if row else None
