from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_mesh.shared.constants import DEFAULT_LLM_MODELS

logger = logging.getLogger(__name__)


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
        migrations_dir = Path(__file__).parent / "migrations"
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
            await self._create_schema(conn)
            await self._ensure_user_schema_upgrade(conn)
            await self._ensure_agent_schema_upgrade(conn)
            await self._ensure_task_schema_upgrade(conn)
            await self._ensure_session_secret(conn)
            await self._ensure_settings(conn)
            await self._ensure_default_templates(conn)
            await self._ensure_admin_user(conn)
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

    # ------------------------------------------------------------------
    # Schema bootstrap (single one-off connection)
    # ------------------------------------------------------------------
    async def _create_schema(self, conn: Any) -> None:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                token_hash TEXT UNIQUE,
                role TEXT DEFAULT 'user',
                disabled BOOLEAN NOT NULL DEFAULT FALSE,
                created_by TEXT,
                last_login_at TIMESTAMP,
                token_created_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS agents (
                id SERIAL PRIMARY KEY,
                agent_id TEXT NOT NULL,
                device_id TEXT UNIQUE,
                alias TEXT,
                runtime TEXT,
                hostname TEXT,
                os TEXT,
                arch TEXT,
                version TEXT,
                online BOOLEAN DEFAULT FALSE,
                last_seen_at TIMESTAMP,
                current_task_id TEXT,
                metadata JSONB,
                llm_api_key TEXT,
                llm_base_url TEXT,
                llm_model TEXT,
                description TEXT,
                system_prompt TEXT,
                access JSONB,
                token_hash TEXT UNIQUE,
                upgrade_requested BOOLEAN NOT NULL DEFAULT FALSE,
                upgrade_version TEXT,
                upgrade_requested_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_agents_agent_id ON agents(agent_id);

            CREATE TABLE IF NOT EXISTS agent_users (
                agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (agent_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                mode TEXT DEFAULT 'llm',
                instruction TEXT NOT NULL,
                workdir TEXT DEFAULT '.',
                timeout_s INTEGER DEFAULT 300,
                model TEXT,
                output_limit INTEGER DEFAULT 200000,
                status TEXT DEFAULT 'queued',
                max_retries INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                assigned_at TIMESTAMP,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                depends_on JSONB,
                dispatched_by TEXT,
                user_id TEXT,
                team_id TEXT,
                metadata JSONB,
                session_id TEXT,
                skills JSONB,
                attachments JSONB
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_agent_id ON tasks(agent_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

            CREATE TABLE IF NOT EXISTS task_queue (
                id SERIAL PRIMARY KEY,
                task_id TEXT UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL,
                enqueued_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_task_queue_agent_id ON task_queue(agent_id);

            CREATE TABLE IF NOT EXISTS task_results (
                task_id TEXT UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                mode TEXT DEFAULT 'llm',
                exit_code INTEGER,
                stdout_tail TEXT,
                stderr_tail TEXT,
                duration_ms INTEGER,
                summary TEXT,
                session_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                size INTEGER,
                content_type TEXT,
                storage_path TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS skills (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                filename TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT,
                md5 TEXT NOT NULL,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS task_logs (
                id SERIAL PRIMARY KEY,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
        """)

    async def _ensure_settings(self, conn: Any) -> None:
        for key, value in (
            ("public_url", ""),
            ("llm_api_key", ""),
            ("llm_base_url", ""),
            ("llm_model", ""),
            ("llm_models", DEFAULT_LLM_MODELS),
            ("config_version", "0"),
            ("auto_upgrade", "1"),
        ):
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO NOTHING",
                key, value,
            )
        # The former global system prompt was superseded by node templates
        # (nodes get their prompt from the node + template layers).
        await conn.execute("DELETE FROM settings WHERE key = 'system_prompt'")

    async def _ensure_default_templates(self, conn: Any) -> None:
        """Seed the built-in permission templates + global default (idempotent)."""
        from agent_mesh.shared import permissions

        for name in permissions.PROFILES:
            permission = permissions.expand_profile(name)
            await conn.execute(
                "INSERT INTO templates (name, description, permission) "
                "VALUES ($1, $2, $3::jsonb) ON CONFLICT (name) DO NOTHING",
                name,
                permissions.PROFILE_DESCRIPTIONS.get(name, ""),
                permissions.dumps(permission),
            )
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('default_permission', $1) "
            "ON CONFLICT (key) DO NOTHING",
            permissions.dumps(permissions.default_permission()),
        )

    async def _ensure_session_secret(self, conn: Any) -> None:
        from agent_mesh.orchestrator.auth import generate_token

        row = await conn.fetchrow(
            "SELECT value FROM settings WHERE key = 'session_secret'"
        )
        if not row:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('session_secret', $1)",
                generate_token(),
            )

    async def _ensure_user_schema_upgrade(self, conn: Any) -> None:
        """Bring an existing users table (created before migration 006) up to date."""
        from agent_mesh.orchestrator.auth import hash_token

        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            )
        }
        if "token_hash" not in cols:
            await conn.execute("ALTER TABLE users ADD COLUMN token_hash TEXT")
        for col in ("disabled", "created_by", "last_login_at", "token_created_at"):
            if col not in cols:
                sql = {
                    "disabled": "ALTER TABLE users ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT FALSE",
                    "created_by": "ALTER TABLE users ADD COLUMN created_by TEXT",
                    "last_login_at": "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP",
                    "token_created_at": "ALTER TABLE users ADD COLUMN token_created_at TIMESTAMP",
                }[col]
                await conn.execute(sql)
        if "token" in cols:
            await conn.execute(
                "UPDATE users SET token_hash = encode(sha256(token::bytea), 'hex') "
                "WHERE token_hash IS NULL AND token IS NOT NULL"
            )
            await conn.execute("ALTER TABLE users DROP COLUMN token")

    async def _ensure_agent_schema_upgrade(self, conn: Any) -> None:
        """Add agent upgrade/config-sync columns to an existing PG schema."""
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                node_description TEXT,
                system_prompt TEXT,
                llm_model TEXT,
                permission JSONB,
                owner_user_id TEXT,
                owner_team_id TEXT,
                data JSONB,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        tpl_cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'templates'"
            )
        }
        if "permission" not in tpl_cols:
            await conn.execute("ALTER TABLE templates ADD COLUMN permission JSONB")
        if "node_description" not in tpl_cols:
            await conn.execute("ALTER TABLE templates ADD COLUMN node_description TEXT")
        if "owner_user_id" not in tpl_cols:
            await conn.execute("ALTER TABLE templates ADD COLUMN owner_user_id TEXT")
        if "owner_team_id" not in tpl_cols:
            await conn.execute("ALTER TABLE templates ADD COLUMN owner_team_id TEXT")
        if "allowed_tools" in tpl_cols:
            await conn.execute("ALTER TABLE templates DROP COLUMN allowed_tools")
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'agents'"
            )
        }
        additions = {
            "arch": "ALTER TABLE agents ADD COLUMN arch TEXT",
            "distro": "ALTER TABLE agents ADD COLUMN distro TEXT",
            "version": "ALTER TABLE agents ADD COLUMN version TEXT",
            "upgrade_requested": "ALTER TABLE agents ADD COLUMN upgrade_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "upgrade_version": "ALTER TABLE agents ADD COLUMN upgrade_version TEXT",
            "upgrade_requested_at": "ALTER TABLE agents ADD COLUMN upgrade_requested_at TIMESTAMP",
            "cpu_percent": "ALTER TABLE agents ADD COLUMN cpu_percent REAL",
            "mem_percent": "ALTER TABLE agents ADD COLUMN mem_percent REAL",
            "mem_used_mb": "ALTER TABLE agents ADD COLUMN mem_used_mb REAL",
            "mem_total_mb": "ALTER TABLE agents ADD COLUMN mem_total_mb REAL",
            "description": "ALTER TABLE agents ADD COLUMN description TEXT",
            "system_prompt": "ALTER TABLE agents ADD COLUMN system_prompt TEXT",
            "access": "ALTER TABLE agents ADD COLUMN access JSONB",
            "template_id": "ALTER TABLE agents ADD COLUMN template_id INTEGER REFERENCES templates(id) ON DELETE SET NULL",
        }
        for col, sql in additions.items():
            if col not in cols:
                await conn.execute(sql)
        if "token_hash" not in cols:
            await conn.execute("ALTER TABLE agents ADD COLUMN token_hash TEXT UNIQUE")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_users (
                agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (agent_id, user_id)
            )
        """)
        # Teams/groups + membership (a user belongs to at most one team).
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                description TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS team_members (
                team_id TEXT NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (team_id, user_id)
            )
        """)
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id)"
        )

    async def _ensure_task_schema_upgrade(self, conn: Any) -> None:
        """Add task columns (attachments, and any older-task columns) to an existing PG schema."""
        cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
            )
        }
        for col, sql in {
            "session_id": "ALTER TABLE tasks ADD COLUMN session_id TEXT",
            "skills": "ALTER TABLE tasks ADD COLUMN skills JSONB",
            "attachments": "ALTER TABLE tasks ADD COLUMN attachments JSONB",
            "user_id": "ALTER TABLE tasks ADD COLUMN user_id TEXT",
            "team_id": "ALTER TABLE tasks ADD COLUMN team_id TEXT",
        }.items():
            if col not in cols:
                await conn.execute(sql)
        if "allowed_tools" in cols:
            await conn.execute("ALTER TABLE tasks DROP COLUMN allowed_tools")

        res_cols = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'task_results'"
            )
        }
        if "session_id" not in res_cols:
            await conn.execute(
                "ALTER TABLE task_results ADD COLUMN session_id TEXT"
            )

        # Audit events. Created by a SQLite migration historically, but the PG
        # schema bootstrap never included it; ensure it exists on both.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_events (
                event_id SERIAL PRIMARY KEY,
                task_id TEXT,
                event_type TEXT NOT NULL,
                agent_id TEXT,
                user_id TEXT,
                details JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, event_id)"
        )

    async def _ensure_admin_user(self, conn: Any) -> None:
        from datetime import datetime, timezone

        from agent_mesh.orchestrator.auth import generate_token, hash_password, hash_token

        row = await conn.fetchrow(
            "SELECT password_hash, token_hash FROM users WHERE username = 'admin'"
        )
        if not row:
            return
        current_hash = row["password_hash"]
        # Reset only the placeholder/invalid hash, never a password the operator changed.
        if not current_hash or current_hash.startswith("$2b$12$placeholder"):
            new_hash = hash_password("admin")
            await conn.execute(
                "UPDATE users SET password_hash = $1 WHERE username = 'admin'",
                new_hash,
            )
            logger.info("reset default admin password hash")
        # Bootstrap a random admin API token (returned once, only stored hashed).
        token_hash = row["token_hash"]
        if not token_hash or token_hash == hash_token("admin-token-change-me"):
            new_token = generate_token()
            await conn.execute(
                "UPDATE users SET token_hash = $1, token_created_at = $2 "
                "WHERE username = 'admin'",
                hash_token(new_token),
                datetime.now(timezone.utc),
            )
            logger.warning(
                "generated a new admin API token (shown once): %s", new_token
            )
