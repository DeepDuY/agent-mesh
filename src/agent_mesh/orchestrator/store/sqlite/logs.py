from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import SQLiteBase


class TaskLogMixin(SQLiteBase):
    async def append_task_logs(
        self, task_id: str, entries: list[dict[str, str]]
    ) -> None:
        if not entries:
            return
        rows = [(task_id, e.get("kind", "raw"), e.get("content", "")) for e in entries]
        await self._execute(
            "INSERT INTO task_logs (task_id, kind, content) VALUES "
            + ", ".join("(?, ?, ?)" for _ in rows),
            tuple(x for r in rows for x in r),
        )

    async def list_task_logs(
        self, task_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = await self._execute(
            "SELECT id, kind, content, created_at FROM task_logs "
            "WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (task_id, after_id, limit),
        )
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def delete_task_logs(self, task_id: str) -> None:
        await self._execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
