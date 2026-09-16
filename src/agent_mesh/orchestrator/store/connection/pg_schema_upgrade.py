"""PostgreSQL column-level schema upgrades for pre-existing databases.

These run on every startup and are idempotent: they inspect
``information_schema.columns`` and add/rename/drop columns so an older PG schema
catches up with the current model without a full migration runner.
"""

from __future__ import annotations

from typing import Any


async def ensure_user_schema_upgrade(conn: Any) -> None:
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
    for col in ("disabled", "created_by", "last_login_at", "token_created_at", "token_expires_at"):
        if col not in cols:
            sql = {
                "disabled": "ALTER TABLE users ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT FALSE",
                "created_by": "ALTER TABLE users ADD COLUMN created_by TEXT",
                "last_login_at": "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP",
                "token_created_at": "ALTER TABLE users ADD COLUMN token_created_at TIMESTAMP",
                "token_expires_at": "ALTER TABLE users ADD COLUMN token_expires_at TIMESTAMP",
            }[col]
            await conn.execute(sql)
    if "token" in cols:
        await conn.execute(
            "UPDATE users SET token_hash = encode(sha256(token::bytea), 'hex') "
            "WHERE token_hash IS NULL AND token IS NOT NULL"
        )
        await conn.execute("ALTER TABLE users DROP COLUMN token")


async def ensure_agent_schema_upgrade(conn: Any) -> None:
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
        "ip_address": "ALTER TABLE agents ADD COLUMN ip_address TEXT",
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


async def ensure_task_schema_upgrade(conn: Any) -> None:
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
