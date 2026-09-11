"""Shared test fixtures.

The orchestrator's runtime storage is SQLite or PostgreSQL; tests use a
throwaway SQLite file. ``sqlite_client`` seeds the schema and pins a
deterministic admin API token, then hands the app a *fresh* connection layer so
no ``asyncio.Lock`` is shared across event loops (the fixture loop vs. the
TestClient portal loop).
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import AsyncIterator

import pytest_asyncio
from fastapi.testclient import TestClient

from agent_mesh.orchestrator.artifact_store import ArtifactStore
from agent_mesh.orchestrator.auth import hash_token
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.main import create_app
from agent_mesh.orchestrator.store.sqlite import SQLiteStore

ADMIN_API_TOKEN = "admin-api-token-123"
ADMIN_USER_ID = "u-admin"
GLOBAL_TOKEN = "mcp-global-token"


async def init_sqlite_file(db_path: str) -> None:
    """Create the schema and pin a deterministic admin API token."""
    backend = SQLiteStore(db_path)
    await backend.initialize()
    # Tests exercise llm dispatch, which is force-configured (no hardcoded
    # fallback model), so seed a default model that is on the seeded allow-list.
    await backend.set_setting("llm_model", "anthropic/deepseek-v4-flash")
    await backend.close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE users SET token_hash = ? WHERE username = 'admin'",
            (hash_token(ADMIN_API_TOKEN),),
        )
        conn.commit()
    finally:
        conn.close()


def publish_fake_bootstrap(db_path: str) -> None:
    """Write a fake bootstrap package next to the DB for upgrade tests."""
    from agent_mesh.shared.constants import VERSION

    bootstrap_dir = Path(db_path).parent / "bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    (bootstrap_dir / "VERSION").write_text(VERSION, encoding="utf-8")
    (bootstrap_dir / "agent-mesh-agent-linux-x64.tar.gz").write_bytes(b"pkg")


@contextlib.asynccontextmanager
async def sqlite_client(
    tmp_path,
    *,
    db_path: str | None = None,
    artifact_dir: str = ":memory:",
    bootstrap: bool = False,
) -> AsyncIterator[TestClient]:
    db_path = db_path or str(tmp_path / "agent-mesh.db")
    await init_sqlite_file(db_path)
    if bootstrap:
        publish_fake_bootstrap(db_path)
    config = OrchestratorConfig(token=GLOBAL_TOKEN, db_path=db_path)
    app, _, _ = create_app(config, artifact_store=ArtifactStore(artifact_dir))
    with TestClient(app) as c:
        yield c


@pytest_asyncio.fixture
async def client(tmp_path):
    async with sqlite_client(tmp_path) as c:
        yield c
