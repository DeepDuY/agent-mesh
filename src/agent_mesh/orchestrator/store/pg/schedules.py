from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_mesh.orchestrator.store.connection import (
    _dump_json,
    _iso_to_dt,
    _load_json,
    _utcnow,
)
from agent_mesh.orchestrator.store.pg.base import PostgresBase


def _row_to_schedule(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["constraints"] = _load_json(data.get("constraints")) or {}
    data["attachments"] = _load_json(data.get("attachments")) or []
    for key in ("next_run_at", "last_run_at", "created_at", "updated_at"):
        data[key] = _iso_to_dt(data.get(key))
    return data


class ScheduleMixin(PostgresBase):
    """Scheduled / recurring tasks."""

    async def create_schedule(
        self,
        *,
        name: str,
        cron: str,
        agent_ref: str,
        instruction: str,
        mode: str = "llm",
        enabled: bool = True,
        timezone: str | None = None,
        constraints: dict[str, Any] | None = None,
        attachments: list[str] | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
        created_by: str | None = None,
        next_run_at: datetime | None = None,
    ) -> int:
        now = _utcnow()
        row = await self._db.fetchrow(
            """
            INSERT INTO schedules (
                name, cron, timezone, enabled, agent_ref, mode, instruction,
                constraints, attachments, user_id, team_id, created_by,
                next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                name,
                cron,
                timezone,
                enabled,
                agent_ref,
                mode,
                instruction,
                _dump_json(constraints or {}),
                _dump_json(attachments or []),
                user_id,
                team_id,
                created_by,
                next_run_at,
                now,
                now,
            ),
        )
        return row["id"]

    async def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        row = await self._db.fetchrow("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
        return _row_to_schedule(dict(row)) if row else None

    async def get_schedule_by_name(self, name: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow("SELECT * FROM schedules WHERE name = ?", (name,))
        return _row_to_schedule(dict(row)) if row else None

    async def list_schedules(self) -> list[dict[str, Any]]:
        rows = await self._db.execute("SELECT * FROM schedules ORDER BY name ASC")
        return [_row_to_schedule(dict(row)) for row in rows]

    async def update_schedule(self, schedule_id: int, **fields: Any) -> bool:
        allowed = {
            "name", "cron", "timezone", "enabled", "agent_ref", "mode",
            "instruction", "constraints", "attachments", "user_id", "team_id",
            "next_run_at", "last_run_at", "last_task_id", "last_status",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("constraints", "attachments"):
                sets.append(f"{key}=?::jsonb")
                params.append(_dump_json(value))
            else:
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_utcnow())
        params.append(schedule_id)
        count = await self._db.execute_rowcount(
            f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        return count > 0

    async def delete_schedule(self, schedule_id: int) -> bool:
        count = await self._db.execute_rowcount(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,)
        )
        return count > 0

    async def list_due_schedules(
        self, now: datetime, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = await self._db.execute(
            "SELECT * FROM schedules "
            "WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= ? "
            "ORDER BY next_run_at ASC LIMIT ?",
            (now, limit),
        )
        return [_row_to_schedule(dict(row)) for row in rows]

    async def claim_schedule(
        self, schedule_id: int, expected_next: datetime | None, new_next: datetime | None
    ) -> bool:
        count = await self._db.execute_rowcount(
            "UPDATE schedules SET next_run_at=?, updated_at=? "
            "WHERE id=? AND next_run_at=?",
            (new_next, _utcnow(), schedule_id, expected_next),
        )
        return count == 1

    async def record_schedule_run(
        self,
        schedule_id: int,
        *,
        last_run_at: datetime,
        last_task_id: str | None,
        last_status: str,
    ) -> None:
        await self._db.execute(
            "UPDATE schedules SET last_run_at=?, last_task_id=?, last_status=?, updated_at=? "
            "WHERE id=?",
            (last_run_at, last_task_id, last_status, _utcnow(), schedule_id),
        )
