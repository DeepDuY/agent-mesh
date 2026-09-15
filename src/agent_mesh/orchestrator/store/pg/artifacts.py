from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.pg.base import PostgresBase


class ArtifactMixin(PostgresBase):
    """Task artifact metadata (binary lives on disk in ArtifactStore)."""

    async def save_artifact(
        self,
        task_id: str,
        artifact_id: str,
        filename: str,
        size: int,
        content_type: str,
        storage_path: str,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO artifacts (artifact_id, task_id, filename, size, content_type, storage_path)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (artifact_id) DO NOTHING
            """,
            (artifact_id, task_id, filename, size, content_type, storage_path),
        )

    async def list_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self._db.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at", (task_id,)
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
