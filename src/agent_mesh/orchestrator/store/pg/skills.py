from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.connection import _utcnow
from agent_mesh.orchestrator.store.pg.base import PostgresBase


class SkillsMixin(PostgresBase):
    """Skills library metadata (name/description/version/enabled/filename)."""

    async def upsert_skill(
        self,
        name: str,
        description: str,
        version: int,
        enabled: bool,
        filename: str,
    ) -> None:
        now = _utcnow()
        await self._db.execute(
            """
            INSERT INTO skills (name, description, version, enabled, filename, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                description=EXCLUDED.description,
                version=EXCLUDED.version,
                enabled=EXCLUDED.enabled,
                filename=EXCLUDED.filename,
                updated_at=EXCLUDED.updated_at
            """,
            (name, description, version, enabled, filename, now),
        )

    async def get_skill(self, name: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow("SELECT * FROM skills WHERE name = ?", (name,))
        return self._row_to_skill(dict(row)) if row else None

    async def list_skills(self) -> list[dict[str, Any]]:
        rows = await self._db.execute("SELECT * FROM skills ORDER BY name ASC")
        return [self._row_to_skill(dict(row)) for row in rows]

    async def update_skill(
        self,
        name: str,
        description: str | None = None,
        version: int | None = None,
        enabled: bool | None = None,
        filename: str | None = None,
    ) -> bool:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_utcnow()]
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if version is not None:
            sets.append("version = ?")
            params.append(version)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(enabled)
        if filename is not None:
            sets.append("filename = ?")
            params.append(filename)
        params.append(name)
        return await self._db.execute_rowcount(
            f"UPDATE skills SET {', '.join(sets)} WHERE name = ?",
            tuple(params),
        ) > 0

    async def delete_skill(self, name: str) -> bool:
        return await self._db.execute_rowcount(
            "DELETE FROM skills WHERE name = ?", (name,)
        ) == 1

    @staticmethod
    def _row_to_skill(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "enabled": bool(row["enabled"]),
            "filename": row["filename"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
