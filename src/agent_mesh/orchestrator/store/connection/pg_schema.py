"""PostgreSQL schema bootstrap: initial DDL and seed data.

Column-level upgrades for pre-existing databases live in
:mod:`agent_mesh.orchestrator.store.connection.pg_schema_upgrade`.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_mesh.shared.constants import DEFAULT_LLM_MODELS

logger = logging.getLogger(__name__)


async def create_schema(conn: Any) -> None:
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
            token_expires_at TIMESTAMP,
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

        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            cron TEXT NOT NULL,
            timezone TEXT,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            agent_ref TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'llm',
            instruction TEXT NOT NULL,
            constraints JSONB,
            attachments JSONB,
            user_id TEXT,
            team_id TEXT,
            created_by TEXT,
            next_run_at TIMESTAMP,
            last_run_at TIMESTAMP,
            last_task_id TEXT,
            last_status TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run_at);
    """)


async def ensure_settings(conn: Any) -> None:
    for key, value in (
        ("public_url", ""),
        ("llm_api_key", ""),
        ("llm_base_url", ""),
        ("llm_model", ""),
        ("llm_models", DEFAULT_LLM_MODELS),
        ("config_version", "0"),
        ("auto_upgrade", "1"),
        # Runtime-configurable upload / transfer limits (see migration 020).
        ("file_max_size_mb", "100"),
        ("artifact_max_size_mb", "100"),
        ("artifact_task_total_mb", "100"),
        ("artifact_total_mb", "200"),
        ("artifact_evict_oldest", "1"),
        ("artifact_timeout_s", "300"),
        # Probe package distribution (see migration 021).
        ("bootstrap_download_base", ""),
        ("probe_release_repo", "DeepDuY/agent-mesh-edge"),
        ("probe_release_token", ""),
        # Scheduled-task timezone (empty = system timezone).
        ("schedule_timezone", ""),
    ):
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO NOTHING",
            key, value,
        )
    # The former global system prompt was superseded by node templates
    # (nodes get their prompt from the node + template layers).
    await conn.execute("DELETE FROM settings WHERE key = 'system_prompt'")


async def ensure_default_templates(conn: Any) -> None:
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


async def ensure_session_secret(conn: Any) -> None:
    from agent_mesh.orchestrator.auth import generate_token

    row = await conn.fetchrow(
        "SELECT value FROM settings WHERE key = 'session_secret'"
    )
    if not row:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('session_secret', $1)",
            generate_token(),
        )


async def ensure_admin_user(conn: Any) -> None:
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
