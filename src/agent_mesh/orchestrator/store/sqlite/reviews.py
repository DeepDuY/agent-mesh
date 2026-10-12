from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.connection import _dump_json, _load_json
from agent_mesh.orchestrator.store.sqlite.connection import SQLiteBase


def _text(value: Any) -> str | None:
    """Coerce a value for a TEXT column (edge/agent refs may arrive as ints)."""
    return None if value is None else str(value)


class ReviewMixin(SQLiteBase):
    """AI review gate audit trail (``task_reviews``)."""

    async def create_task_review(
        self,
        *,
        task_id: str,
        agent_id: str | None = None,
        mode: str | None = None,
        score: int | None = None,
        threshold: int | None = None,
        verdict: str | None = None,
        action: str | None = None,
        reason: str | None = None,
        violations: list[Any] | None = None,
        model: str | None = None,
        source: str | None = None,
        actor: str | None = None,
    ) -> int:
        rows = await self._execute(
            """
            INSERT INTO task_reviews (
                task_id, agent_id, mode, score, threshold, verdict, action,
                reason, violations, model, source, actor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                str(task_id),
                _text(agent_id),
                _text(mode),
                score,
                threshold,
                _text(verdict),
                _text(action),
                _text(reason),
                _dump_json(violations or []),
                _text(model),
                _text(source),
                _text(actor),
            ),
        )
        return int(rows[0]["id"]) if rows else 0

    async def set_task_review(
        self, task_id: str, score: int | None, reason: str | None
    ) -> None:
        await self._execute(
            "UPDATE tasks SET review_score = ?, review_reason = ? WHERE task_id = ?",
            (score, reason, task_id),
        )

    async def list_task_reviews(
        self,
        task_id: str | None = None,
        *,
        action: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._reviews_where(task_id, action, search)
        sql = f"SELECT * FROM task_reviews{where} ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self._execute(sql, tuple(params))
        return [self._review_row(r) for r in rows]

    async def count_task_reviews(
        self,
        task_id: str | None = None,
        *,
        action: str | None = None,
        search: str | None = None,
    ) -> int:
        where, params = self._reviews_where(task_id, action, search)
        rows = await self._execute(
            f"SELECT COUNT(*) AS n FROM task_reviews{where}", tuple(params)
        )
        return int(rows[0]["n"]) if rows else 0

    @staticmethod
    def _reviews_where(
        task_id: str | None, action: str | None, search: str | None
    ) -> tuple[str, list[Any]]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if task_id:
            where += " AND task_id = ?"
            params.append(task_id)
        if action:
            where += " AND action = ?"
            params.append(action)
        if search:
            like = f"%{search}%"
            where += (
                " AND (task_id LIKE ? OR agent_id LIKE ? OR reason LIKE ?"
                " OR actor LIKE ? OR model LIKE ?)"
            )
            params.extend([like, like, like, like, like])
        return where, params

    @staticmethod
    def _review_row(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "mode": row["mode"],
            "score": row["score"],
            "threshold": row["threshold"],
            "verdict": row["verdict"],
            "action": row["action"],
            "reason": row["reason"],
            "violations": _load_json(row["violations"]) or [],
            "model": row["model"],
            "source": row["source"],
            "actor": row["actor"],
            "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
        }
