from __future__ import annotations

from datetime import datetime, timezone

from agent_mesh.orchestrator.store.sqlite.connection import _dt_to_iso, SQLiteBase


class SettingsMixin(SQLiteBase):
    async def get_setting(self, key: str) -> str | None:
        rows = await self._execute("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, _dt_to_iso(datetime.now(timezone.utc))),
        )

    async def list_settings(self) -> dict[str, str]:
        rows = await self._execute("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}
