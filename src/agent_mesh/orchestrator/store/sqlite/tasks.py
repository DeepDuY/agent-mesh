from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import _dt_to_iso, _dump_json, _iso_to_dt, _load_json, SQLiteBase
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import ArtifactRef, Constraints, FileRef, Task, TaskResult


class TaskMixin(SQLiteBase):
    def _row_to_task(self, row: dict[str, Any]) -> Task:
        constraints = Constraints(
            workdir=row["workdir"],
            timeout_s=row["timeout_s"],
            model=row["model"],
            allowed_tools=_load_json(row["allowed_tools"]),
            output_limit=row["output_limit"],
            session_id=row["session_id"] if "session_id" in row.keys() else None,
            skills=_load_json(row["skills"]) if "skills" in row.keys() else None,
        )
        attachments = (
            [FileRef(**a) for a in _load_json(row["attachments"])]
            if "attachments" in row.keys() and _load_json(row["attachments"])
            else []
        )
        return Task(
            task_id=row["task_id"],
            agent_id=row["agent_id"],
            mode=row["mode"] or "llm",
            instruction=row["instruction"],
            constraints=constraints,
            status=TaskStatus(row["status"]),
            created_at=_iso_to_dt(row["created_at"]) or datetime.now(timezone.utc),
            assigned_at=_iso_to_dt(row["assigned_at"]),
            started_at=_iso_to_dt(row["started_at"]),
            finished_at=_iso_to_dt(row["finished_at"]),
            retry_count=row["retry_count"],
            result=None,
            max_retries=row["max_retries"],
            attachments=attachments,
        )

    async def _load_result(self, task_id: str) -> TaskResult | None:
        rows = await self._execute(
            "SELECT * FROM task_results WHERE task_id = ?", (task_id,)
        )
        if not rows:
            return None
        row = rows[0]
        artifacts = await self.list_artifacts(task_id)
        return TaskResult(
            status=row["status"],
            mode=row["mode"] or "llm",
            exit_code=row["exit_code"],
            stdout_tail=row["stdout_tail"] or "",
            stderr_tail=row["stderr_tail"] or "",
            artifacts=[ArtifactRef(**a) for a in artifacts],
            duration_ms=row["duration_ms"] or 0,
            summary=row["summary"] or "",
            session_id=row["session_id"] if "session_id" in row.keys() else None,
        )

    async def create_task(
        self,
        task: Task,
        dispatched_by: str | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT INTO tasks (
                task_id, agent_id, mode, instruction, workdir, timeout_s, model,
                allowed_tools, output_limit, status, max_retries, retry_count,
                created_at, assigned_at, started_at, finished_at, depends_on,
                dispatched_by, metadata, session_id, skills, attachments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.task_id, task.agent_id, task.mode, task.instruction,
                task.constraints.workdir, task.constraints.timeout_s,
                task.constraints.model,
                _dump_json(task.constraints.allowed_tools),
                task.constraints.output_limit,
                task.status.value,
                task.max_retries,
                task.retry_count,
                _dt_to_iso(task.created_at),
                _dt_to_iso(task.assigned_at),
                _dt_to_iso(task.started_at),
                _dt_to_iso(task.finished_at),
                _dump_json(None),
                dispatched_by,
                _dump_json(None),
                task.constraints.session_id,
                _dump_json(task.constraints.skills),
                _dump_json([a.model_dump() for a in task.attachments]),
            ),
        )

    async def get_task(self, task_id: str) -> Task | None:
        rows = await self._execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        )
        if not rows:
            return None
        task = self._row_to_task(rows[0])
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
            where += " AND (instruction LIKE ? OR task_id LIKE ? OR agent_id LIKE ?)"
            params.extend([like, like, like])
        if started_after is not None:
            where += " AND started_at >= ?"
            params.append(_dt_to_iso(started_after))
        if started_before is not None:
            where += " AND started_at <= ?"
            params.append(_dt_to_iso(started_before))
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
    ) -> list[Task]:
        where, params = self._tasks_where(
            agent_id, status, mode, search, started_after, started_before
        )
        sql = f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = await self._execute(sql, tuple(params))
        tasks = [self._row_to_task(row) for row in rows]
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
    ) -> int:
        where, params = self._tasks_where(
            agent_id, status, mode, search, started_after, started_before
        )
        sql = f"SELECT COUNT(*) AS n FROM tasks{where}"
        rows = await self._execute(sql, tuple(params))
        return int(rows[0]["n"]) if rows else 0

    async def delete_task(self, task_id: str) -> bool:
        # task_queue/task_results/artifacts cascade via FK, but task_logs has no
        # FK constraint (migration 012) so it must be removed explicitly — same
        # as delete_tasks and the PG backend.
        await self._execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
        affected = await self._execute_rowcount(
            "DELETE FROM tasks WHERE task_id = ?", (task_id,)
        )
        return affected > 0

    async def delete_tasks(self, task_ids: list[str]) -> int:
        if not task_ids:
            return 0
        placeholders = ",".join("?" * len(task_ids))
        # task_queue/task_results/artifacts/task_logs cascade in SQLite, but
        # delete explicitly so behaviour is identical across backends.
        await self._execute(
            f"DELETE FROM task_queue WHERE task_id IN ({placeholders})",
            tuple(task_ids),
        )
        await self._execute(
            f"DELETE FROM task_results WHERE task_id IN ({placeholders})",
            tuple(task_ids),
        )
        await self._execute(
            f"DELETE FROM artifacts WHERE task_id IN ({placeholders})",
            tuple(task_ids),
        )
        await self._execute(
            f"DELETE FROM task_logs WHERE task_id IN ({placeholders})",
            tuple(task_ids),
        )
        affected = await self._execute_rowcount(
            f"DELETE FROM tasks WHERE task_id IN ({placeholders})",
            tuple(task_ids),
        )
        return affected

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
        sql = "UPDATE tasks SET status = ?"
        params: list[Any] = [status]
        if assigned_at is not None:
            sql += ", assigned_at = ?"
            params.append(_dt_to_iso(assigned_at))
        if started_at is not None:
            sql += ", started_at = ?"
            params.append(_dt_to_iso(started_at))
        if finished_at is not None:
            sql += ", finished_at = ?"
            params.append(_dt_to_iso(finished_at))
        if retry_count is not None:
            sql += ", retry_count = ?"
            params.append(retry_count)
        sql += " WHERE task_id = ?"
        params.append(task_id)
        if expected_status is not None:
            sql += " AND status = ?"
            params.append(expected_status)
        affected = await self._execute_rowcount(sql, tuple(params))
        return affected > 0

    async def set_task_result(
        self, task_id: str, result: TaskResult
    ) -> bool:
        affected = await self._execute_rowcount(
            """
            INSERT INTO task_results (task_id, status, mode, exit_code, stdout_tail, stderr_tail, duration_ms, summary, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status=excluded.status,
                mode=excluded.mode,
                exit_code=excluded.exit_code,
                stdout_tail=excluded.stdout_tail,
                stderr_tail=excluded.stderr_tail,
                duration_ms=excluded.duration_ms,
                summary=excluded.summary,
                session_id=excluded.session_id
            """,
            (
                task_id, result.status, result.mode, result.exit_code,
                result.stdout_tail, result.stderr_tail,
                result.duration_ms, result.summary, result.session_id,
            ),
        )
        return affected > 0


class QueueMixin(SQLiteBase):
    async def enqueue(self, task_id: str, agent_id: str) -> None:
        await self._execute(
            """
            INSERT INTO task_queue (task_id, agent_id) VALUES (?, ?)
            ON CONFLICT(task_id) DO UPDATE SET agent_id=excluded.agent_id, enqueued_at=excluded.enqueued_at
            """,
            (task_id, agent_id),
        )

    async def dequeue(self, agent_id: str) -> str | None:
        return await self._db.dequeue(agent_id)

    async def remove_from_queue(self, task_id: str) -> None:
        await self._execute("DELETE FROM task_queue WHERE task_id = ?", (task_id,))
