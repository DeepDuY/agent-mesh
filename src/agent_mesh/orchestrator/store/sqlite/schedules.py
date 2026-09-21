from __future__ import annotations

from datetime import datetime, timezone as _tz
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import (
    _dt_to_iso,
    _dump_json,
    _iso_to_dt,
    _load_json,
    SQLiteBase,
)

_SCHEDULE_COLUMNS = (
    "id, name, cron, timezone, enabled, agent_ref, mode, instruction, "
    "constraints, attachments, user_id, team_id, created_by, "
    "next_run_at, last_run_at, last_task_id, last_status, created_at, updated_at"
)


def _row_to_schedule(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    data["constraints"] = _load_json(data.get("constraints")) or {}
    data["attachments"] = _load_json(data.get("attachments")) or []
    for key in ("next_run_at", "last_run_at", "created_at", "updated_at"):
        data[key] = _iso_to_dt(data.get(key))
    return data


class ScheduleMixin(SQLiteBase):
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
        now = datetime.now(_tz.utc)
        rows = await self._execute(
            """
            INSERT INTO schedules (
                name, cron, timezone, enabled, agent_ref, mode, instruction,
                constraints, attachments, user_id, team_id, created_by,
                next_run_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                name,
                cron,
                timezone,
                1 if enabled else 0,
                agent_ref,
                mode,
                instruction,
                _dump_json(constraints or {}),
                _dump_json(attachments or []),
                user_id,
                team_id,
                created_by,
                _dt_to_iso(next_run_at),
                _dt_to_iso(now),
                _dt_to_iso(now),
            ),
        )
        return rows[0]["id"]

    async def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        rows = await self._execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE id = ?", (schedule_id,)
        )
        return _row_to_schedule(rows[0]) if rows else None

    async def get_schedule_by_name(self, name: str) -> dict[str, Any] | None:
        rows = await self._execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM schedules WHERE name = ?", (name,)
        )
        return _row_to_schedule(rows[0]) if rows else None

    async def list_schedules(self) -> list[dict[str, Any]]:
        rows = await self._execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM schedules ORDER BY name ASC"
        )
        return [_row_to_schedule(row) for row in rows]

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
                value = _dump_json(value)
            elif key == "enabled":
                value = 1 if value else 0
            elif key in ("next_run_at", "last_run_at"):
                value = _dt_to_iso(value)
            sets.append(f"{key}=?")
            params.append(value)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_dt_to_iso(datetime.now(_tz.utc)))
        params.append(schedule_id)
        count = await self._execute_rowcount(
            f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        return count > 0

    async def delete_schedule(self, schedule_id: int) -> bool:
        count = await self._execute_rowcount(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,)
        )
        return count > 0

    async def list_due_schedules(
        self, now: datetime, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = await self._execute(
            f"SELECT {_SCHEDULE_COLUMNS} FROM schedules "
            "WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ? "
            "ORDER BY next_run_at ASC LIMIT ?",
            (_dt_to_iso(now), limit),
        )
        return [_row_to_schedule(row) for row in rows]

    async def claim_schedule(
        self, schedule_id: int, expected_next: datetime | None, new_next: datetime | None
    ) -> bool:
        """Atomically advance ``next_run_at``; False if another ticker won."""
        count = await self._execute_rowcount(
            "UPDATE schedules SET next_run_at=?, updated_at=? "
            "WHERE id=? AND next_run_at=?",
            (
                _dt_to_iso(new_next),
                _dt_to_iso(datetime.now(_tz.utc)),
                schedule_id,
                _dt_to_iso(expected_next),
            ),
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
        await self._execute(
            "UPDATE schedules SET last_run_at=?, last_task_id=?, last_status=?, updated_at=? "
            "WHERE id=?",
            (
                _dt_to_iso(last_run_at),
                last_task_id,
                last_status,
                _dt_to_iso(datetime.now(_tz.utc)),
                schedule_id,
            ),
        )
