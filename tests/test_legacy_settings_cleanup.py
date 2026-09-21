"""Legacy global prompt cleanup.

Migration 013 seeded ``settings.system_prompt`` and 014 dropped it; the SQLite
connection layer also deletes it on every startup (mirroring Postgres
``ensure_settings``) so legacy/hand-edited databases are cleaned too. See
docs/known-issues.md (former §7).
"""

from __future__ import annotations

import asyncio
import sqlite3

from agent_mesh.orchestrator.store.sqlite import SQLiteStore


def _keys(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT key FROM settings")]
    finally:
        conn.close()


def test_fresh_sqlite_has_no_global_system_prompt(tmp_path):
    db_path = str(tmp_path / "fresh.db")

    async def _init():
        store = SQLiteStore(db_path)
        await store.initialize()
        await store.close()

    asyncio.run(_init())
    assert "system_prompt" not in _keys(db_path)


def test_sqlite_drops_legacy_global_system_prompt(tmp_path):
    db_path = str(tmp_path / "legacy.db")

    async def _init():
        store = SQLiteStore(db_path)
        await store.initialize()
        await store.close()

    asyncio.run(_init())

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO settings (key, value) VALUES ('system_prompt', 'legacy')")
    conn.commit()
    conn.close()
    assert "system_prompt" in _keys(db_path)

    asyncio.run(_init())
    assert "system_prompt" not in _keys(db_path)
