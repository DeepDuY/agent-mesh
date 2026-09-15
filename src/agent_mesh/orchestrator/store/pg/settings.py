from __future__ import annotations

from agent_mesh.orchestrator.store.connection import _utcnow
from agent_mesh.orchestrator.store.pg.base import PostgresBase


class SettingsMixin(PostgresBase):
    """Global settings key/value store."""

    async def get_setting(self, key: str) -> str | None:
        row = await self._db.fetchrow("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET
                value=EXCLUDED.value,
                updated_at=EXCLUDED.updated_at
            """,
            (key, value, _utcnow()),
        )

    async def list_settings(self) -> dict[str, str]:
        rows = await self._db.execute("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}
