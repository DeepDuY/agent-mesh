from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.pg.base import PostgresBase


class FileMixin(PostgresBase):
    """File library metadata (binary lives on disk)."""

    async def create_file(
        self,
        file_id: str,
        filename: str,
        size: int,
        content_type: str,
        md5: str,
        created_by: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO files (file_id, filename, size, content_type, md5, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, filename, size, content_type, md5, created_by),
        )

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        )
        return dict(row) if row else None

    async def get_files_by_ids(self, file_ids: list[str]) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        rows = await self._db.execute(
            "SELECT * FROM files WHERE file_id = ANY(?::text[])",
            (file_ids,),
        )
        return [dict(row) for row in rows]

    async def find_file_by_md5(
        self, md5: str, filename: str, owner: str | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM files WHERE md5 = ? AND filename = ?"
        params: list[Any] = [md5, filename]
        if owner is not None:
            sql += " AND created_by = ?"
            params.append(owner)
        row = await self._db.fetchrow(sql, tuple(params))
        return dict(row) if row else None

    async def list_files(
        self, search: str | None = None, owner: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM files WHERE 1=1"
        params: list[Any] = []
        if owner is not None:
            sql += " AND created_by = ?"
            params.append(owner)
        if search:
            like = f"%{search}%"
            sql += " AND (filename ILIKE ? OR file_id ILIKE ?)"
            params.extend([like, like])
        sql += " ORDER BY created_at DESC"
        rows = await self._db.execute(sql, tuple(params))
        return [dict(row) for row in rows]

    async def delete_file(self, file_id: str) -> bool:
        return await self._db.execute_rowcount(
            "DELETE FROM files WHERE file_id = ?", (file_id,)
        ) == 1
