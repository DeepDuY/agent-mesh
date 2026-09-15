from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from agent_mesh.orchestrator.store.base import AbstractStore
from agent_mesh.orchestrator.store.connection import (
    _dump_json,
    _iso_to_dt,
    _load_json,
    _utcnow,
    PostgresDatabase,
)
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import (
    METRIC_FIELDS,
    SYSTEM_FIELDS,
    TELEMETRY_FIELDS,
    AgentStatus,
    ArtifactRef,
    Constraints,
    FileRef,
    Task,
    TaskResult,
    validate_telemetry,
)

logger = logging.getLogger(__name__)


def _row_to_template(row: dict[str, Any]) -> dict[str, Any]:
    row["permission"] = _load_json(row.get("permission"))
    row["data"] = _load_json(row.get("data"))
    return row


class PostgresStore(AbstractStore):
    """PostgreSQL store backed by the unified :class:`PostgresDatabase` layer.

    Connection lifecycle (per-process lazy pool) and schema bootstrap live in
    ``store.connection.PostgresDatabase``; this class only does row mapping and
    SQL.
    """

    def __init__(
        self,
        *,
        dsn: str = "",
        host: str = "127.0.0.1",
        port: int = 5432,
        user: str = "agent_mesh",
        password: str = "",
        database: str = "agent_mesh",
        min_size: int = 2,
        max_size: int = 10,
    ):
        self._db = PostgresDatabase(
            dsn=dsn,
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            min_size=min_size,
            max_size=max_size,
        )

    async def initialize(self) -> None:
        await self._db.initialize()

    async def close(self) -> None:
        await self._db.close()

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    def _row_to_agent(self, row: dict[str, Any]) -> AgentStatus:
        return AgentStatus(
            id=row["id"],
            agent_id=row["agent_id"],
            device_id=row["device_id"],
            alias=row["alias"],
            runtime=row["runtime"],
            hostname=row["hostname"],
            os=row["os"],
            distro=row.get("distro"),
            arch=row.get("arch"),
            version=row.get("version"),
            online=bool(row["online"]),
            last_seen=_iso_to_dt(row["last_seen_at"]),
            current_task_id=row["current_task_id"],
            llm_api_key=row.get("llm_api_key"),
            llm_base_url=row.get("llm_base_url"),
            llm_model=row.get("llm_model"),
            description=row.get("description"),
            system_prompt=row.get("system_prompt"),
            template_id=row.get("template_id"),
            access=_load_json(row.get("access")),
            upgrade_requested=bool(row.get("upgrade_requested")),
            upgrade_version=row.get("upgrade_version"),
            cpu_percent=row.get("cpu_percent"),
            mem_percent=row.get("mem_percent"),
            mem_used_mb=row.get("mem_used_mb"),
            mem_total_mb=row.get("mem_total_mb"),
        )

    async def upsert_agent(
        self,
        agent_id: str,
        device_id: str | None,
        runtime: str | None,
        hostname: str | None,
        online: bool,
        last_seen: datetime | None,
        current_task_id: str | None,
        metadata: dict[str, Any] | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        version: str | None = None,
        telemetry: dict[str, Any] | None = None,
    ) -> int:
        telemetry = dict(telemetry or {})
        validate_telemetry(telemetry)
        now = _utcnow()
        existing = await self.get_agent(device_id) if device_id else await self.get_agent_by_agent_id(agent_id)
        if existing:
            sets = [
                "agent_id=?", "runtime=?", "hostname=?", "online=?",
                "last_seen_at=?", "current_task_id=?", "metadata=?",
                "version=?", "updated_at=?",
            ]
            params: list[Any] = [
                agent_id, runtime, hostname, online,
                last_seen, current_task_id,
                _dump_json(metadata), version, now,
            ]
            for col in SYSTEM_FIELDS:
                if col in telemetry:
                    params.append(telemetry[col])
                    sets.append(f"{col}=COALESCE(?,{col})")
            for col in METRIC_FIELDS:
                if col in telemetry:
                    params.append(telemetry[col])
                    sets.append(f"{col}=?")
            params.append(existing.id)
            await self._db.execute(
                f"UPDATE agents SET {', '.join(sets)} WHERE id=?",
                tuple(params),
            )
            return existing.id

        cols = [
            "agent_id", "device_id", "runtime", "hostname",
            "online", "last_seen_at", "current_task_id", "metadata",
            "version", "updated_at",
        ]
        vals: list[Any] = [
            agent_id, device_id, runtime, hostname, online,
            last_seen, current_task_id,
            _dump_json(metadata), version, now,
        ]
        for col in TELEMETRY_FIELDS:
            if col in telemetry:
                cols.append(col)
                vals.append(telemetry[col])
        placeholders = ", ".join("?" for _ in cols)
        row = await self._db.fetchrow(
            f"INSERT INTO agents ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
            tuple(vals),
        )
        return row["id"]

    async def get_agent(self, device_id: str) -> AgentStatus | None:
        row = await self._db.fetchrow("SELECT * FROM agents WHERE device_id = ?", (device_id,))
        return self._row_to_agent(dict(row)) if row else None

    async def get_agent_by_agent_id(self, agent_id: str) -> AgentStatus | None:
        row = await self._db.fetchrow(
            "SELECT * FROM agents WHERE agent_id = ? ORDER BY updated_at DESC LIMIT 1", (agent_id,)
        )
        return self._row_to_agent(dict(row)) if row else None

    async def get_agent_by_id(self, agent_id: int) -> AgentStatus | None:
        row = await self._db.fetchrow("SELECT * FROM agents WHERE id = ?", (agent_id,))
        return self._row_to_agent(dict(row)) if row else None

    async def list_agents(self) -> list[AgentStatus]:
        # Stable registration order: `updated_at` is bumped on every heartbeat
        # (see upsert_agent), so ordering by it reshuffles the fleet every poll.
        rows = await self._db.execute("SELECT * FROM agents ORDER BY id ASC")
        return [self._row_to_agent(dict(row)) for row in rows]

    async def set_agent_online(self, device_id: str, online: bool) -> None:
        # Match by device_id, or by agent_id when the row has no device_id (a
        # legacy/abnormal node would otherwise never be flipped offline).
        await self._db.execute(
            "UPDATE agents SET online = ?, updated_at = ? "
            "WHERE device_id = ? OR (device_id IS NULL AND agent_id = ?)",
            (online, _utcnow(), device_id, device_id),
        )

    async def set_agent_current_task(self, device_id: str, task_id: str | None) -> None:
        await self._db.execute(
            "UPDATE agents SET current_task_id = ?, updated_at = ? "
            "WHERE device_id = ? OR (device_id IS NULL AND agent_id = ?)",
            (task_id, _utcnow(), device_id, device_id),
        )

    async def set_agent_alias_by_id(self, agent_id: int, alias: str | None) -> None:
        await self._db.execute(
            "UPDATE agents SET alias = ?, updated_at = ? WHERE id = ?",
            (alias, _utcnow(), agent_id),
        )

    async def set_agent_description_by_id(
        self, agent_id: int, description: str | None
    ) -> None:
        await self._db.execute(
            "UPDATE agents SET description = ?, updated_at = ? WHERE id = ?",
            (description, _utcnow(), agent_id),
        )

    async def set_agent_system_prompt_by_id(
        self, agent_id: int, system_prompt: str | None
    ) -> None:
        await self._db.execute(
            "UPDATE agents SET system_prompt = ?, updated_at = ? WHERE id = ?",
            (system_prompt, _utcnow(), agent_id),
        )

    async def set_agent_template_by_id(
        self, agent_id: int, template_id: int | None
    ) -> None:
        await self._db.execute(
            "UPDATE agents SET template_id = ?, updated_at = ? WHERE id = ?",
            (template_id, _utcnow(), agent_id),
        )

    # ------------------------------------------------------------------
    # Node templates
    # ------------------------------------------------------------------
    async def create_template(
        self,
        name: str,
        description: str | None = None,
        node_description: str | None = None,
        system_prompt: str | None = None,
        llm_model: str | None = None,
        permission: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> int:
        row = await self._db.fetchrow(
            """
            INSERT INTO templates
                (name, description, node_description, system_prompt, llm_model,
                 permission, data, owner_user_id, owner_team_id)
            VALUES (?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, ?, ?) RETURNING id
            """,
            (
                name,
                description,
                node_description,
                system_prompt,
                llm_model,
                _dump_json(permission),
                _dump_json(data),
                owner_user_id,
                owner_team_id,
            ),
        )
        return row["id"]

    async def get_template(self, template_id: int) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        )
        return _row_to_template(dict(row)) if row else None

    async def get_template_by_name(self, name: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT * FROM templates WHERE name = ?", (name,)
        )
        return _row_to_template(dict(row)) if row else None

    async def list_templates(self) -> list[dict[str, Any]]:
        rows = await self._db.execute("SELECT * FROM templates ORDER BY name ASC")
        return [_row_to_template(dict(row)) for row in rows]

    async def update_template(self, template_id: int, **fields: Any) -> bool:
        allowed = {
            "name",
            "description",
            "node_description",
            "system_prompt",
            "llm_model",
            "permission",
            "data",
            "owner_user_id",
            "owner_team_id",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("permission", "data"):
                sets.append(f"{key}=?::jsonb")
                params.append(_dump_json(value))
            else:
                sets.append(f"{key}=?")
                params.append(value)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_utcnow())
        params.append(template_id)
        count = await self._db.execute_rowcount(
            f"UPDATE templates SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        return count > 0

    async def delete_template(self, template_id: int) -> bool:
        count = await self._db.execute_rowcount(
            "DELETE FROM templates WHERE id = ?", (template_id,)
        )
        return count > 0

    async def delete_agent(self, agent_id: int) -> bool:
        return await self._db.execute_rowcount(
            "DELETE FROM agents WHERE id = ?", (agent_id,)
        ) > 0

    async def set_agent_llm_config(
        self,
        agent_id: int,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        await self._db.execute(
            "UPDATE agents SET llm_api_key = ?, llm_base_url = ?, llm_model = ?, updated_at = ? WHERE id = ?",
            (llm_api_key, llm_base_url, llm_model, _utcnow(), agent_id),
        )

    async def get_agent_token_hash(self, agent_id: int) -> str | None:
        row = await self._db.fetchrow(
            "SELECT token_hash FROM agents WHERE id = ?", (agent_id,)
        )
        return row["token_hash"] if row else None

    async def set_agent_token_hash(self, agent_id: int, token_hash: str | None) -> None:
        await self._db.execute(
            "UPDATE agents SET token_hash = ?, updated_at = ? WHERE id = ?",
            (token_hash, _utcnow(), agent_id),
        )

    async def get_agent_by_token_hash(self, token_hash: str) -> AgentStatus | None:
        row = await self._db.fetchrow(
            "SELECT * FROM agents WHERE token_hash = ? LIMIT 1", (token_hash,)
        )
        return self._row_to_agent(dict(row)) if row else None

    async def add_agent_user(self, agent_id: int, user_id: str) -> None:
        await self._db.execute(
            "INSERT INTO agent_users (agent_id, user_id) VALUES (?, ?) "
            "ON CONFLICT (agent_id, user_id) DO NOTHING",
            (agent_id, user_id),
        )

    async def list_agent_users(self, agent_id: int) -> list[str]:
        rows = await self._db.execute(
            "SELECT user_id FROM agent_users WHERE agent_id = ?", (agent_id,)
        )
        return [r["user_id"] for r in rows]

    async def set_agent_access_by_id(
        self, agent_id: int, access: dict[str, Any] | None
    ) -> None:
        await self._db.execute(
            "UPDATE agents SET access = ?::jsonb, updated_at = ? WHERE id = ?",
            (_dump_json(access), _utcnow(), agent_id),
        )

    # ------------------------------------------------------------------
    # Teams / groups
    # ------------------------------------------------------------------
    async def create_team(
        self, team_id: str, name: str, description: str | None = None
    ) -> None:
        await self._db.execute(
            "INSERT INTO teams (team_id, name, description) VALUES (?, ?, ?)",
            (team_id, name, description),
        )

    async def get_team(self, team_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT team_id, name, description, created_at, updated_at "
            "FROM teams WHERE team_id = ?",
            (team_id,),
        )
        return dict(row) if row else None

    async def get_team_by_name(self, name: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT team_id, name, description, created_at, updated_at "
            "FROM teams WHERE name = ?",
            (name,),
        )
        return dict(row) if row else None

    async def list_teams(self) -> list[dict[str, Any]]:
        rows = await self._db.execute(
            "SELECT team_id, name, description, created_at, updated_at "
            "FROM teams ORDER BY name ASC"
        )
        return [dict(r) for r in rows]

    async def update_team(
        self,
        team_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(_utcnow())
        params.append(team_id)
        return await self._db.execute_rowcount(
            f"UPDATE teams SET {', '.join(sets)} WHERE team_id = ?", tuple(params)
        ) > 0

    async def delete_team(self, team_id: str) -> bool:
        await self._db.execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
        # Detach the deleted team from tasks and node ACLs so it does not linger
        # as a dangling reference (ghost team pointer).
        await self._db.execute("UPDATE tasks SET team_id = NULL WHERE team_id = ?", (team_id,))
        for agent in await self.list_agents():
            access = dict(agent.access or {})
            teams = access.get("teams") or []
            remaining = [t for t in teams if t != team_id]
            if remaining != teams:
                access["teams"] = remaining
                await self.set_agent_access_by_id(agent.id, access)
        return await self._db.execute_rowcount(
            "DELETE FROM teams WHERE team_id = ?", (team_id,)
        ) > 0

    async def list_team_members(self, team_id: str) -> list[str]:
        rows = await self._db.execute(
            "SELECT user_id FROM team_members WHERE team_id = ? ORDER BY user_id",
            (team_id,),
        )
        return [r["user_id"] for r in rows]

    async def set_user_team(self, user_id: str, team_id: str | None) -> None:
        await self._db.execute(
            "DELETE FROM team_members WHERE user_id = ?", (user_id,)
        )
        if team_id:
            await self._db.execute(
                "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                (team_id, user_id),
            )

    async def get_user_team(self, user_id: str) -> str | None:
        row = await self._db.fetchrow(
            "SELECT team_id FROM team_members WHERE user_id = ?", (user_id,)
        )
        return row["team_id"] if row else None

    async def request_agent_upgrade(self, agent_id: int, version: str) -> bool:
        now = _utcnow()
        return await self._db.execute_rowcount(
            "UPDATE agents SET upgrade_requested = TRUE, upgrade_version = ?, upgrade_requested_at = ?, updated_at = ? WHERE id = ?",
            (version, now, now, agent_id),
        ) > 0

    async def clear_agent_upgrade(self, agent_id: int) -> None:
        await self._db.execute(
            "UPDATE agents SET upgrade_requested = FALSE, upgrade_version = NULL, upgrade_requested_at = NULL, updated_at = ? WHERE id = ?",
            (_utcnow(), agent_id),
        )

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------
    async def upsert_skill(
        self,
        name: str,
        description: str,
        version: int,
        enabled: bool,
        filename: str,
    ) -> None:
        now = _utcnow()
        await self._db.execute(
            """
            INSERT INTO skills (name, description, version, enabled, filename, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                description=EXCLUDED.description,
                version=EXCLUDED.version,
                enabled=EXCLUDED.enabled,
                filename=EXCLUDED.filename,
                updated_at=EXCLUDED.updated_at
            """,
            (name, description, version, enabled, filename, now),
        )

    async def get_skill(self, name: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow("SELECT * FROM skills WHERE name = ?", (name,))
        return self._row_to_skill(dict(row)) if row else None

    async def list_skills(self) -> list[dict[str, Any]]:
        rows = await self._db.execute("SELECT * FROM skills ORDER BY name ASC")
        return [self._row_to_skill(dict(row)) for row in rows]

    async def update_skill(
        self,
        name: str,
        description: str | None = None,
        version: int | None = None,
        enabled: bool | None = None,
        filename: str | None = None,
    ) -> bool:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_utcnow()]
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if version is not None:
            sets.append("version = ?")
            params.append(version)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(enabled)
        if filename is not None:
            sets.append("filename = ?")
            params.append(filename)
        params.append(name)
        return await self._db.execute_rowcount(
            f"UPDATE skills SET {', '.join(sets)} WHERE name = ?",
            tuple(params),
        ) > 0

    async def delete_skill(self, name: str) -> bool:
        return await self._db.execute_rowcount(
            "DELETE FROM skills WHERE name = ?", (name,)
        ) == 1

    @staticmethod
    def _row_to_skill(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "version": row["version"],
            "enabled": bool(row["enabled"]),
            "filename": row["filename"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Task logs (live execution stream)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # File library
    # ------------------------------------------------------------------
    async def create_file(
        self,
        file_id: str,
        filename: str,
        size: int,
        content_type: str,
        md5: str,
        created_by: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO files (file_id, filename, size, content_type, md5, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, filename, size, content_type, md5, created_by),
        )

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        )
        return dict(row) if row else None

    async def get_files_by_ids(self, file_ids: list[str]) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        rows = await self._db.execute(
            "SELECT * FROM files WHERE file_id = ANY(?::text[])",
            (file_ids,),
        )
        return [dict(row) for row in rows]

    async def find_file_by_md5(
        self, md5: str, filename: str, owner: str | None = None
    ) -> dict[str, Any] | None:
        sql = "SELECT * FROM files WHERE md5 = ? AND filename = ?"
        params: list[Any] = [md5, filename]
        if owner is not None:
            sql += " AND created_by = ?"
            params.append(owner)
        row = await self._db.fetchrow(sql, tuple(params))
        return dict(row) if row else None

    async def list_files(
        self, search: str | None = None, owner: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM files WHERE 1=1"
        params: list[Any] = []
        if owner is not None:
            sql += " AND created_by = ?"
            params.append(owner)
        if search:
            like = f"%{search}%"
            sql += " AND (filename ILIKE ? OR file_id ILIKE ?)"
            params.extend([like, like])
        sql += " ORDER BY created_at DESC"
        rows = await self._db.execute(sql, tuple(params))
        return [dict(row) for row in rows]

    async def delete_file(self, file_id: str) -> bool:
        return await self._db.execute_rowcount(
            "DELETE FROM files WHERE file_id = ?", (file_id,)
        ) == 1

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    async def get_setting(self, key: str) -> str | None:
        row = await self._db.fetchrow("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET
                value=EXCLUDED.value,
                updated_at=EXCLUDED.updated_at
            """,
            (key, value, _utcnow()),
        )

    async def list_settings(self) -> dict[str, str]:
        rows = await self._db.execute("SELECT key, value FROM settings")
        return {row["key"]: row["value"] for row in rows}

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    async def create_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        token_hash: str,
        role: str = "user",
        disabled: bool = False,
        created_by: str | None = None,
        token_created_at: datetime | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO users (user_id, username, password_hash, token_hash, role, disabled, created_by, token_created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, password_hash, token_hash, role, disabled, created_by,
             token_created_at),
        )

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT user_id, username, password_hash, role, disabled, created_by, created_at, last_login_at, token_created_at, token_hash "
            "FROM users WHERE username = ?",
            (username,),
        )
        return dict(row) if row else None

    async def get_user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT user_id, username, password_hash, role, disabled, created_by, created_at, last_login_at, token_created_at, token_hash "
            "FROM users WHERE token_hash = ?",
            (token_hash,),
        )
        return dict(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        rows = await self._db.execute(
            "SELECT user_id, username, role, disabled, created_by, created_at, last_login_at, token_created_at "
            "FROM users ORDER BY created_at ASC"
        )
        return [dict(row) for row in rows]

    async def delete_user(self, user_id: str) -> bool:
        # Clean up memberships so no "ghost" team member / node-operator rows
        # linger after the user row is gone.
        await self._db.execute("DELETE FROM team_members WHERE user_id = ?", (user_id,))
        await self._db.execute("DELETE FROM agent_users WHERE user_id = ?", (user_id,))
        return await self._db.execute_rowcount(
            "DELETE FROM users WHERE user_id = ?", (user_id,)
        ) == 1

    async def set_user_password(self, user_id: str, password_hash: str) -> bool:
        return await self._db.execute_rowcount(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (password_hash, user_id),
        ) == 1

    async def set_user_token(
        self,
        user_id: str,
        token_hash: str,
        token_created_at: datetime | None = None,
    ) -> bool:
        return await self._db.execute_rowcount(
            "UPDATE users SET token_hash = ?, token_created_at = ? WHERE user_id = ?",
            (token_hash, token_created_at, user_id),
        ) == 1

    async def set_user_last_login(
        self, user_id: str, last_login_at: datetime | None
    ) -> None:
        await self._db.execute(
            "UPDATE users SET last_login_at = ? WHERE user_id = ?",
            (last_login_at, user_id),
        )
