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
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import (
    ArtifactRef,
    Constraints,
    FileRef,
    Task,
    TaskResult,
)


class TaskMixin(PostgresBase):
    """Tasks table + task_results + audit events."""

    def _row_to_task(self, row: dict[str, Any]) -> Task:
        constraints = Constraints(
            workdir=row["workdir"],
            timeout_s=row["timeout_s"],
            model=row["model"],
            output_limit=row.get("output_limit", 200_000),
            session_id=row.get("session_id"),
            skills=_load_json(row.get("skills")),
        )
        attachments = [
            FileRef(**a) for a in (_load_json(row.get("attachments")) or [])
        ]
        return Task(
            task_id=row["task_id"],
            agent_id=row["agent_id"],
            mode=row.get("mode") or "llm",
            instruction=row["instruction"],
            constraints=constraints,
            status=TaskStatus(row["status"]),
            user_id=row.get("user_id"),
            team_id=row.get("team_id"),
            created_at=_iso_to_dt(row.get("created_at")) or _utcnow(),
            assigned_at=_iso_to_dt(row.get("assigned_at")),
            started_at=_iso_to_dt(row.get("started_at")),
            finished_at=_iso_to_dt(row.get("finished_at")),
            retry_count=row.get("retry_count", 0),
            result=None,
            max_retries=row.get("max_retries", 0),
            attachments=attachments,
        )

    async def _load_result(self, task_id: str) -> TaskResult | None:
        row = await self._db.fetchrow("SELECT * FROM task_results WHERE task_id = ?", (task_id,))
        if not row:
            return None
        artifacts = await self.list_artifacts(task_id)
        return TaskResult(
            status=row["status"],
            mode=row.get("mode") or "llm",
            exit_code=row["exit_code"],
            stdout_tail=row.get("stdout_tail") or "",
            stderr_tail=row.get("stderr_tail") or "",
            artifacts=[ArtifactRef(**a) for a in artifacts],
            duration_ms=row.get("duration_ms") or 0,
            summary=row.get("summary") or "",
            session_id=row.get("session_id"),
        )

    async def create_task(
        self,
        task: Task,
        dispatched_by: str | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO tasks (
                task_id, agent_id, mode, instruction, workdir, timeout_s, model,
                output_limit, status, max_retries, retry_count,
                created_at, assigned_at, started_at, finished_at, depends_on,
                dispatched_by, user_id, team_id, metadata, session_id, skills, attachments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id, task.agent_id, task.mode, task.instruction,
                task.constraints.workdir, task.constraints.timeout_s,
                task.constraints.model,
                task.constraints.output_limit,
                task.status.value,
                task.max_retries,
                task.retry_count,
                task.created_at,
                task.assigned_at,
                task.started_at,
                task.finished_at,
                _dump_json(None),
                dispatched_by,
                user_id,
                team_id,
                _dump_json(None),
                task.constraints.session_id,
                _dump_json(task.constraints.skills),
                _dump_json([a.model_dump() for a in task.attachments]),
            ),
        )

    async def get_task(self, task_id: str) -> Task | None:
        row = await self._db.fetchrow("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if not row:
            return None
        task = self._row_to_task(dict(row))
        task.result = await self._load_result(task_id)
        return task

    @staticmethod
    def _tasks_where(
        agent_id: str | None,
        status: str | None,
        mode: str | None,
        search: str | None,
        started_after: datetime | None,
        started_before: datetime | None,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        where = " WHERE 1=1"
        params: list[Any] = []
        if agent_id:
            where += " AND agent_id = ?"
            params.append(agent_id)
        if status:
            where += " AND status = ?"
            params.append(status)
        if mode:
            where += " AND mode = ?"
            params.append(mode)
        if search:
            like = f"%{search}%"
            where += " AND (instruction ILIKE ? OR task_id ILIKE ? OR agent_id ILIKE ?)"
            params.extend([like, like, like])
        if started_after is not None:
            where += " AND started_at >= ?"
            params.append(started_after)
        if started_before is not None:
            where += " AND started_at <= ?"
            params.append(started_before)
        if owner_user_id is not None or owner_team_id is not None:
            clauses = []
            if owner_user_id is not None:
                clauses.append("user_id = ?")
                params.append(owner_user_id)
            if owner_team_id is not None:
                clauses.append("team_id = ?")
                params.append(owner_team_id)
            where += " AND (" + " OR ".join(clauses) + ")"
        return where, params

    async def list_tasks(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        search: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> list[Task]:
        where, params = self._tasks_where(
            agent_id, status, mode, search, started_after, started_before,
            owner_user_id, owner_team_id,
        )
        sql = f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self._db.execute(sql, tuple(params))
        tasks = [self._row_to_task(dict(row)) for row in rows]
        for t in tasks:
            t.result = await self._load_result(t.task_id)
        return tasks

    async def count_tasks(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        search: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> int:
        where, params = self._tasks_where(
            agent_id, status, mode, search, started_after, started_before,
            owner_user_id, owner_team_id,
        )
        row = await self._db.fetchrow(f"SELECT COUNT(*) AS n FROM tasks{where}", tuple(params))
        return int(row["n"]) if row else 0

    async def delete_task(self, task_id: str) -> bool:
        await self._db.execute("DELETE FROM task_queue WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM task_results WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM artifacts WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
        return await self._db.execute_rowcount(
            "DELETE FROM tasks WHERE task_id = ?", (task_id,)
        ) == 1

    async def delete_tasks(self, task_ids: list[str]) -> int:
        if not task_ids:
            return 0
        params_list = [(t,) for t in task_ids]
        await self._db.executemany("DELETE FROM task_queue WHERE task_id = ?", params_list)
        await self._db.executemany("DELETE FROM task_results WHERE task_id = ?", params_list)
        await self._db.executemany("DELETE FROM artifacts WHERE task_id = ?", params_list)
        await self._db.executemany("DELETE FROM task_logs WHERE task_id = ?", params_list)
        return await self._db.execute_rowcount(
            "DELETE FROM tasks WHERE task_id = ANY(?::text[])",
            (task_ids,),
        )

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        assigned_at: datetime | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        retry_count: int | None = None,
        expected_status: str | None = None,
    ) -> bool:
        parts = ["status = ?"]
        params: list[Any] = [status]
        if assigned_at is not None:
            parts.append("assigned_at = ?")
            params.append(assigned_at)
        if started_at is not None:
            parts.append("started_at = ?")
            params.append(started_at)
        if finished_at is not None:
            parts.append("finished_at = ?")
            params.append(finished_at)
        if retry_count is not None:
            parts.append("retry_count = ?")
            params.append(retry_count)
        sql = f"UPDATE tasks SET {', '.join(parts)} WHERE task_id = ?"
        params.append(task_id)
        if expected_status is not None:
            sql += " AND status = ?"
            params.append(expected_status)
        return await self._db.execute_rowcount(sql, tuple(params)) > 0

    async def set_task_result(self, task_id: str, result: TaskResult) -> bool:
        return await self._db.execute_rowcount(
            """
            INSERT INTO task_results (task_id, status, mode, exit_code, stdout_tail, stderr_tail, duration_ms, summary, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (task_id) DO UPDATE SET
                status=EXCLUDED.status,
                mode=EXCLUDED.mode,
                exit_code=EXCLUDED.exit_code,
                stdout_tail=EXCLUDED.stdout_tail,
                stderr_tail=EXCLUDED.stderr_tail,
                duration_ms=EXCLUDED.duration_ms,
                summary=EXCLUDED.summary,
                session_id=EXCLUDED.session_id
            """,
            (
                task_id, result.status, result.mode, result.exit_code,
                result.stdout_tail, result.stderr_tail,
                result.duration_ms, result.summary, result.session_id,
            ),
        ) > 0

    async def append_task_event(
        self,
        task_id: str,
        event_type: str,
        agent_id: str | None = None,
        user_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO task_events (task_id, event_type, agent_id, user_id, details) "
            "VALUES (?, ?, ?, ?, ?::jsonb)",
            (
                task_id,
                event_type,
                str(agent_id) if agent_id is not None else None,
                str(user_id) if user_id is not None else None,
                _dump_json(details),
            ),
        )

    async def list_task_events(
        self, task_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        rows = await self._db.execute(
            "SELECT event_id, task_id, event_type, agent_id, user_id, details, created_at "
            "FROM task_events WHERE task_id = ? ORDER BY event_id ASC LIMIT ?",
            (task_id, limit),
        )
        return [
            {
                "event_id": r["event_id"],
                "task_id": r["task_id"],
                "event_type": r["event_type"],
                "agent_id": r["agent_id"],
                "user_id": r["user_id"],
                "details": _load_json(r["details"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]


class QueueMixin(PostgresBase):
    """Task queue."""

    async def enqueue(self, task_id: str, agent_id: str) -> None:
        await self._db.execute(
            """
            INSERT INTO task_queue (task_id, agent_id) VALUES (?, ?)
            ON CONFLICT (task_id) DO UPDATE SET agent_id=EXCLUDED.agent_id, enqueued_at=NOW()
            """,
            (task_id, agent_id),
        )

    async def dequeue(self, agent_id: str) -> str | None:
        return await self._db.dequeue(agent_id)

    async def remove_from_queue(self, task_id: str) -> None:
        await self._db.execute("DELETE FROM task_queue WHERE task_id = ?", (task_id,))
