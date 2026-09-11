#!/usr/bin/env python3
"""Migrate the agent-mesh SQLite database to PostgreSQL.

Reads the SQLite DB (default: /opt/agent-mesh/data/agent-mesh.db) and copies all
rows into a PostgreSQL database. The PG schema is created by PostgresStore
(including its incremental upgrades), then data is inserted in dependency order.

Usage:
    python scripts/migrate_sqlite_to_pg.py \
        --sqlite /opt/agent-mesh/data/agent-mesh.db \
        --pg-dsn postgresql://agent_mesh:PASS@127.0.0.1:5432/agent_mesh

Idempotent for settings (upsert); other tables are inserted as-is. Run with the
orchestrator stopped, or before it is pointed at PG, to avoid conflicting writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone

import asyncpg


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _ts(value):
    """Convert a DB value to a naive datetime (UTC) for asyncpg's TIMESTAMP
    columns, or None. PG schema uses TIMESTAMP (without time zone); asyncpg
    rejects offset-aware datetimes for such columns."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _dump_json(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _load_all(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return conn.execute(f"SELECT * FROM {table}").fetchall()


async def _create_pg_schema(dsn: str) -> None:
    from agent_mesh.orchestrator.store.pg import PostgresStore

    store = PostgresStore(dsn=dsn)
    await store.initialize()
    await store.close()


async def _insert(
    pool, table: str, columns: list[str], rows: list[tuple],
) -> int:
    if not rows:
        return 0
    col_list = ", ".join(columns)
    placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    async with pool.acquire() as conn:
        await conn.executemany(sql, [tuple(r) for r in rows])
    return len(rows)


def _read(sqlite_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


async def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite -> PostgreSQL")
    parser.add_argument("--sqlite", default="/opt/agent-mesh/data/agent-mesh.db")
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get(
            "AGENT_MESH_PG_DSN",
            "postgresql://agent_mesh:CHANGEME@127.0.0.1:5432/agent_mesh",
        ),
        help="PostgreSQL DSN (defaults to $AGENT_MESH_PG_DSN)",
    )
    args = parser.parse_args()

    sqlite = _read(args.sqlite)
    await _create_pg_schema(args.pg_dsn)
    pool = await asyncpg.create_pool(args.pg_dsn, min_size=1, max_size=5)

    try:
        counts: dict[str, int] = {}

        # users (all columns, as-is)
        rows = _load_all(sqlite, "users")
        cols = [
            "user_id", "username", "password_hash", "token_hash", "role",
            "disabled", "created_by", "last_login_at", "token_created_at",
            "created_at",
        ]
        counts["users"] = await _insert(pool, "users", cols, [
            (r["user_id"], r["username"], r["password_hash"], r["token_hash"],
             r["role"], bool(r["disabled"]), r["created_by"],
             _ts(r["last_login_at"]), _ts(r["token_created_at"]),
             _ts(r["created_at"]))
            for r in rows
        ])

        # agents (map columns explicitly)
        rows = _load_all(sqlite, "agents")
        cols = [
            "id", "agent_id", "device_id", "alias", "runtime", "hostname",
            "os", "distro", "arch", "version", "online", "last_seen_at",
            "current_task_id", "metadata", "llm_api_key", "llm_base_url",
            "llm_model", "token_hash", "upgrade_requested", "upgrade_version",
            "upgrade_requested_at", "cpu_percent", "mem_percent", "mem_used_mb",
            "mem_total_mb", "created_at", "updated_at",
        ]
        counts["agents"] = await _insert(pool, "agents", cols, [
            (r["id"], r["agent_id"], r["device_id"], r["alias"], r["runtime"],
             r["hostname"], r["os"], r["distro"], r["arch"], r["version"],
             bool(r["online"]), _ts(r["last_seen_at"]), r["current_task_id"],
             _dump_json(r["metadata"]), r["llm_api_key"], r["llm_base_url"],
             r["llm_model"], r["token_hash"], bool(r["upgrade_requested"]),
             r["upgrade_version"], _ts(r["upgrade_requested_at"]),
             r["cpu_percent"], r["mem_percent"], r["mem_used_mb"],
             r["mem_total_mb"], _ts(r["created_at"]), _ts(r["updated_at"]))
            for r in rows
        ])

        # agent_users
        rows = _load_all(sqlite, "agent_users")
        counts["agent_users"] = await _insert(pool, "agent_users",
                                              ["agent_id", "user_id", "created_at"],
                                              [(r["agent_id"], r["user_id"], _ts(r["created_at"])) for r in rows])

        # tasks
        rows = _load_all(sqlite, "tasks")
        cols = [
            "task_id", "agent_id", "mode", "instruction", "workdir",
            "timeout_s", "model", "allowed_tools", "output_limit", "status",
            "max_retries", "retry_count", "created_at", "assigned_at",
            "started_at", "finished_at", "depends_on", "dispatched_by",
            "metadata", "session_id", "skills", "attachments",
        ]
        counts["tasks"] = await _insert(pool, "tasks", cols, [
            (r["task_id"], r["agent_id"], r["mode"], r["instruction"],
             r["workdir"], r["timeout_s"], r["model"],
             _dump_json(r["allowed_tools"]), r["output_limit"], r["status"],
             r["max_retries"], r["retry_count"], _ts(r["created_at"]),
             _ts(r["assigned_at"]), _ts(r["started_at"]),
             _ts(r["finished_at"]), _dump_json(r["depends_on"]),
             r["dispatched_by"], _dump_json(r["metadata"]), r["session_id"],
             _dump_json(r["skills"]), _dump_json(r["attachments"]))
            for r in rows
        ])

        # task_queue (skip: 0 rows, but keep for completeness)
        rows = _load_all(sqlite, "task_queue")
        counts["task_queue"] = await _insert(pool, "task_queue",
                                             ["task_id", "agent_id", "enqueued_at"],
                                             [(r["task_id"], r["agent_id"], _ts(r["enqueued_at"])) for r in rows])

        # task_results (SQLite submitted_at -> PG created_at; keep session_id)
        rows = _load_all(sqlite, "task_results")
        counts["task_results"] = await _insert(pool, "task_results",
            ["task_id", "status", "mode", "exit_code", "stdout_tail",
             "stderr_tail", "duration_ms", "summary", "session_id", "created_at"],
            [(r["task_id"], r["status"], r["mode"], r["exit_code"],
              r["stdout_tail"], r["stderr_tail"], r["duration_ms"], r["summary"],
              r["session_id"], _ts(r["submitted_at"]))
             for r in rows])

        # artifacts
        rows = _load_all(sqlite, "artifacts")
        counts["artifacts"] = await _insert(pool, "artifacts",
            ["artifact_id", "task_id", "filename", "size", "content_type",
             "storage_path", "created_at"],
            [(r["artifact_id"], r["task_id"], r["filename"], r["size"],
              r["content_type"], r["storage_path"], _ts(r["created_at"]))
             for r in rows])

        # files
        rows = _load_all(sqlite, "files")
        counts["files"] = await _insert(pool, "files",
            ["file_id", "filename", "size", "content_type", "md5", "created_by", "created_at"],
            [(r["file_id"], r["filename"], r["size"], r["content_type"],
              r["md5"], r["created_by"], _ts(r["created_at"])) for r in rows])

        # skills
        rows = _load_all(sqlite, "skills")
        counts["skills"] = await _insert(pool, "skills",
            ["name", "description", "version", "enabled", "filename", "created_at", "updated_at"],
            [(r["name"], r["description"], r["version"], bool(r["enabled"]),
              r["filename"], _ts(r["created_at"]), _ts(r["updated_at"]))
             for r in rows])

        # settings (upsert to avoid clobbering)
        rows = _load_all(sqlite, "settings")
        async with pool.acquire() as conn:
            for r in rows:
                await conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, $3) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at",
                    r["key"], r["value"], _ts(r["updated_at"]),
                )
        counts["settings"] = len(rows)

        # task_logs
        rows = _load_all(sqlite, "task_logs")
        counts["task_logs"] = await _insert(pool, "task_logs",
            ["id", "task_id", "kind", "content", "created_at"],
            [(r["id"], r["task_id"], r["kind"], r["content"], _ts(r["created_at"])) for r in rows])

        # schema_migrations (keep the same versions so PG reports applied state)
        rows = _load_all(sqlite, "schema_migrations")
        counts["schema_migrations"] = await _insert(pool, "schema_migrations",
                                                    ["version", "applied_at"],
                                                    [(r["version"], _ts(r["applied_at"])) for r in rows])

        # ---- Fix PG sequences (agents.id, task_queue.id, task_logs.id) ----
        async with pool.acquire() as conn:
            await conn.execute("SELECT setval('agents_id_seq', COALESCE((SELECT MAX(id) FROM agents), 1))")
            await conn.execute("SELECT setval('task_queue_id_seq', COALESCE((SELECT MAX(id) FROM task_queue), 1))")
            await conn.execute("SELECT setval('task_logs_id_seq', COALESCE((SELECT MAX(id) FROM task_logs), 1))")

        print("Migration complete:")
        for table, n in counts.items():
            print(f"  {table:20s} {n}")
    finally:
        await pool.close()
        sqlite.close()


if __name__ == "__main__":
    asyncio.run(main())
