from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import SQLiteBase


class FileMixin(SQLiteBase):
    async def create_file(
        self,
        file_id: str,
        filename: str,
        size: int,
        content_type: str,
        md5: str,
        created_by: str | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO files (file_id, filename, size, content_type, md5, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, filename, size, content_type, md5, created_by),
        )

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        rows = await self._execute(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        )
        return dict(rows[0]) if rows else None

    async def get_files_by_ids(self, file_ids: list[str]) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        placeholders = ",".join("?" * len(file_ids))
        rows = await self._execute(
            f"SELECT * FROM files WHERE file_id IN ({placeholders})",
            tuple(file_ids),
        )
        return [dict(r) for r in rows]

    async def find_file_by_md5(
        self, md5: str, filename: str
    ) -> dict[str, Any] | None:
        rows = await self._execute(
            "SELECT * FROM files WHERE md5 = ? AND filename = ?", (md5, filename)
        )
        return dict(rows[0]) if rows else None

    async def list_files(self, search: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM files"
        params: list[Any] = []
        if search:
            like = f"%{search}%"
            sql += " WHERE filename LIKE ? OR file_id LIKE ?"
            params.extend([like, like])
        sql += " ORDER BY created_at DESC"
        rows = await self._execute(sql, tuple(params))
        return [dict(r) for r in rows]

    async def delete_file(self, file_id: str) -> bool:
        affected = await self._execute_rowcount(
            "DELETE FROM files WHERE file_id = ?", (file_id,)
        )
        return affected > 0
