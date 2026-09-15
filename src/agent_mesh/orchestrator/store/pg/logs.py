from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.pg.base import PostgresBase


class TaskLogMixin(PostgresBase):
    """Live task execution log stream."""

    async def append_task_logs(
        self, task_id: str, entries: list[dict[str, str]]
    ) -> None:
        if not entries:
            return
        await self._db.executemany(
            "INSERT INTO task_logs (task_id, kind, content) VALUES (?, ?, ?)",
            [(task_id, e.get("kind", "raw"), e.get("content", "")) for e in entries],
        )

    async def list_task_logs(
        self, task_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = await self._db.execute(
            "SELECT id, kind, content, created_at FROM task_logs "
            "WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (task_id, after_id, limit),
        )
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "content": row["content"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ]

    async def delete_task_logs(self, task_id: str) -> None:
        await self._db.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
