from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import logging

from agent_mesh.orchestrator.store.connection.base import Database, _dt_to_iso

logger = logging.getLogger(__name__)


class SQLiteDatabase(Database):
    """SQLite connection layer: one connection per call + asyncio lock."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock: Any = None

    async def initialize(self) -> None:
        import asyncio

        self._lock = asyncio.Lock()
        self._execute_sync("PRAGMA journal_mode=WAL")
        self._execute_sync("PRAGMA busy_timeout=5000")
        await self._run_migrations()
        await self._ensure_default_templates()
        await self._ensure_session_secret()
        await self._ensure_admin_user()

    async def close(self) -> None:
        pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _execute_sync(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            cur = conn.execute(sql, params or ())
            rows = cur.fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    async def execute(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[Any]:
        async with self._lock:
            return self._execute_sync(sql, params)

    async def execute_rowcount(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> int:
        async with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(sql, params or ())
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def fetchrow(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> Any | None:
        rows = await self.execute(sql, params)
        return rows[0] if rows else None

    async def executemany(
        self, sql: str, params_list: list[tuple[Any, ...]]
    ) -> int:
        if not params_list:
            return 0
        async with self._lock:
            conn = self._connect()
            try:
                cur = conn.executemany(sql, params_list)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def dequeue(self, agent_id: str) -> str | None:
        async with self._lock:
            conn = self._connect()
            try:
                # Atomic dequeue: delete + return in a single statement so two
                # workers polling simultaneously never hand out the same task.
                cur = conn.execute(
                    "DELETE FROM task_queue WHERE task_id = ("
                    "SELECT task_id FROM task_queue WHERE agent_id = ? "
                    "ORDER BY enqueued_at ASC LIMIT 1"
                    ") RETURNING task_id",
                    (agent_id,),
                )
                row = cur.fetchone()
                conn.commit()
                return row["task_id"] if row else None
            finally:
                conn.close()

    async def _run_migrations(self) -> None:
        # Migrations live in the parent ``store/migrations`` directory.
        migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
        files = sorted(migrations_dir.glob("*.sql"))
        for f in files:
            version = int(f.stem.split("_")[0])
            async with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS schema_migrations ("
                        "version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                        ")"
                    )
                    rows = conn.execute(
                        "SELECT version FROM schema_migrations WHERE version = ?",
                        (version,),
                    ).fetchall()
                    if not rows:
                        sql = f.read_text(encoding="utf-8")
                        sql += f"\nINSERT INTO schema_migrations (version) VALUES ({version});\n"
                        conn.executescript(sql)
                        conn.commit()
                        logger.info("applied migration %s", f.name)
                finally:
                    conn.close()

    async def _ensure_default_templates(self) -> None:
        """Seed the built-in permission templates + global default (idempotent)."""
        from agent_mesh.shared import permissions

        for name in permissions.PROFILES:
            permission = permissions.expand_profile(name)
            await self.execute(
                "INSERT OR IGNORE INTO templates (name, description, permission) "
                "VALUES (?, ?, ?)",
                (
                    name,
                    permissions.PROFILE_DESCRIPTIONS.get(name, ""),
                    permissions.dumps(permission),
                ),
            )
        await self.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('default_permission', ?)",
            (permissions.dumps(permissions.default_permission()),),
        )

    async def _ensure_session_secret(self) -> None:
        from agent_mesh.orchestrator.auth import generate_token

        rows = await self.execute(
            "SELECT value FROM settings WHERE key = ?", ("session_secret",)
        )
        if rows:
            return
        await self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("session_secret", generate_token()),
        )

    async def _ensure_admin_user(self) -> None:
        from agent_mesh.orchestrator.auth import (
            generate_token,
            hash_password,
            hash_token,
        )

        rows = await self.execute(
            "SELECT password_hash, token_hash FROM users WHERE username = ?", ("admin",)
        )
        if not rows:
            return
        current_hash = rows[0]["password_hash"]
        # Reset only the placeholder/invalid hash, never a password the operator changed.
        if not current_hash or current_hash.startswith("$2b$12$placeholder"):
            new_hash = hash_password("admin")
            await self.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (new_hash, "admin"),
            )
            logger.info("reset default admin password hash")
        # Bootstrap a random admin API token (returned once, only stored hashed).
        token_hash = rows[0]["token_hash"]
        if not token_hash or token_hash == hash_token("admin-token-change-me"):
            new_token = generate_token()
            await self.execute(
                "UPDATE users SET token_hash = ?, token_created_at = ? WHERE username = ?",
                (hash_token(new_token), _dt_to_iso(datetime.now(timezone.utc)), "admin"),
            )
            logger.warning(
                "generated a new admin API token (shown once): %s", new_token
            )
