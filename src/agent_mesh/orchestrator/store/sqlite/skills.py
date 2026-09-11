from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import SQLiteBase


class SkillsMixin(SQLiteBase):
    async def upsert_skill(
        self,
        name: str,
        description: str,
        version: int,
        enabled: bool,
        filename: str,
    ) -> None:
        now = _dt_to_iso(datetime.now(timezone.utc))
        await self._execute(
            """
            INSERT INTO skills (name, description, version, enabled, filename, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description=excluded.description,
                version=excluded.version,
                enabled=excluded.enabled,
                filename=excluded.filename,
                updated_at=excluded.updated_at
            """,
            (name, description, version, 1 if enabled else 0, filename, now),
        )

    async def get_skill(self, name: str) -> dict[str, Any] | None:
        rows = await self._execute(
            "SELECT * FROM skills WHERE name = ?", (name,)
        )
        return self._row_to_skill(rows[0]) if rows else None

    async def list_skills(self) -> list[dict[str, Any]]:
        rows = await self._execute("SELECT * FROM skills ORDER BY name ASC")
        return [self._row_to_skill(row) for row in rows]

    async def update_skill(
        self,
        name: str,
        description: str | None = None,
        version: int | None = None,
        enabled: bool | None = None,
        filename: str | None = None,
    ) -> bool:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_dt_to_iso(datetime.now(timezone.utc))]
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if version is not None:
            sets.append("version = ?")
            params.append(version)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if filename is not None:
            sets.append("filename = ?")
            params.append(filename)
        params.append(name)
        affected = await self._execute_rowcount(
            f"UPDATE skills SET {', '.join(sets)} WHERE name = ?",
            tuple(params),
        )
        return affected > 0

    async def delete_skill(self, name: str) -> bool:
        affected = await self._execute_rowcount(
            "DELETE FROM skills WHERE name = ?", (name,)
        )
        return affected > 0

    @staticmethod
    def _row_to_skill(row: Any) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "enabled": bool(row["enabled"]),
            "filename": row["filename"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


def _dt_to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
