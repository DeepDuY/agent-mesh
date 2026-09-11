from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import SQLiteBase


class ArtifactMixin(SQLiteBase):
    async def save_artifact(
        self,
        task_id: str,
        artifact_id: str,
        filename: str,
        size: int,
        content_type: str,
        storage_path: str,
    ) -> None:
        await self._execute(
            """
            INSERT OR IGNORE INTO artifacts (artifact_id, task_id, filename, size, content_type, storage_path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (artifact_id, task_id, filename, size, content_type, storage_path),
        )

    async def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
        return [
            {
                "artifact_id": row["artifact_id"],
                "filename": row["filename"],
                "size": row["size"],
                "content_type": row["content_type"],
                "download_url": f"/api/artifacts/{task_id}/{row['artifact_id']}",
            }
            for row in rows
        ]
