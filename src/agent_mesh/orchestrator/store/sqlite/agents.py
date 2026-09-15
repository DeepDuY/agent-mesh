from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import _dt_to_iso, _dump_json, _iso_to_dt, _load_json, SQLiteBase
from agent_mesh.shared.schemas import (
    METRIC_FIELDS,
    SYSTEM_FIELDS,
    TELEMETRY_FIELDS,
    AgentStatus,
    validate_telemetry,
)


def _row_get(row: Any, key: str):
    return row[key] if key in row.keys() else None


class AgentMixin(SQLiteBase):
    def _row_to_agent(self, row: dict[str, Any]) -> AgentStatus:
        return AgentStatus(
            id=row["id"],
            agent_id=row["agent_id"],
            device_id=row["device_id"],
            alias=row["alias"],
            runtime=row["runtime"],
            hostname=row["hostname"],
            os=row["os"],
            distro=_row_get(row, "distro"),
            arch=_row_get(row, "arch"),
            version=_row_get(row, "version"),
            online=bool(row["online"]),
            last_seen=_iso_to_dt(row["last_seen_at"]),
            current_task_id=row["current_task_id"],
            llm_api_key=_row_get(row, "llm_api_key"),
            llm_base_url=_row_get(row, "llm_base_url"),
            llm_model=_row_get(row, "llm_model"),
            description=_row_get(row, "description"),
            system_prompt=_row_get(row, "system_prompt"),
            template_id=_row_get(row, "template_id"),
            access=_load_json(_row_get(row, "access")),
            upgrade_requested=bool(_row_get(row, "upgrade_requested")),
            upgrade_version=_row_get(row, "upgrade_version"),
            cpu_percent=_row_get(row, "cpu_percent"),
            mem_percent=_row_get(row, "mem_percent"),
            mem_used_mb=_row_get(row, "mem_used_mb"),
            mem_total_mb=_row_get(row, "mem_total_mb"),
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
        """Upsert an agent row.

        ``telemetry`` is the registered SYSTEM/METRIC field subset (see
        ``shared.schemas``). Column names are only ever drawn from the field
        registry constants; unknown keys raise ``ValueError``. SYSTEM fields
        keep the previous value when missing/None (COALESCE), METRIC fields are
        always overwritten.
        """
        telemetry = dict(telemetry or {})
        validate_telemetry(telemetry)
        now = _dt_to_iso(datetime.now(timezone.utc))
        existing = await self.get_agent(device_id) if device_id else await self.get_agent_by_agent_id(agent_id)
        if existing:
            sets = [
                "agent_id=?",
                "runtime=?",
                "hostname=?",
                "online=?",
                "last_seen_at=?",
                "current_task_id=?",
                "metadata=?",
                "version=?",
                "updated_at=?",
            ]
            params: list[Any] = [
                agent_id, runtime, hostname, online,
                _dt_to_iso(last_seen), current_task_id,
                _dump_json(metadata), version, now,
            ]
            for col in SYSTEM_FIELDS:
                if col in telemetry:
                    sets.append(f"{col}=COALESCE(?,{col})")
                    params.append(telemetry[col])
            for col in METRIC_FIELDS:
                if col in telemetry:
                    sets.append(f"{col}=?")
                    params.append(telemetry[col])
            params.append(existing.id)
            await self._execute(
                f"UPDATE agents SET {', '.join(sets)} WHERE id=?", tuple(params)
            )
            return existing.id

        cols = [
            "agent_id", "device_id", "runtime", "hostname",
            "online", "last_seen_at", "current_task_id", "metadata",
            "version", "updated_at",
        ]
        vals: list[Any] = [
            agent_id, device_id, runtime, hostname, online,
            _dt_to_iso(last_seen), current_task_id,
            _dump_json(metadata), version, now,
        ]
        for col in TELEMETRY_FIELDS:
            if col in telemetry:
                cols.append(col)
                vals.append(telemetry[col])
        placeholders = ", ".join("?" for _ in cols)
        rows = await self._execute(
            f"INSERT INTO agents ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
            tuple(vals),
        )
        return rows[0]["id"]

    async def get_agent(self, device_id: str) -> AgentStatus | None:
        rows = await self._execute(
            "SELECT * FROM agents WHERE device_id = ?", (device_id,)
        )
        return self._row_to_agent(rows[0]) if rows else None

    async def get_agent_by_agent_id(self, agent_id: str) -> AgentStatus | None:
        rows = await self._execute(
            "SELECT * FROM agents WHERE agent_id = ? ORDER BY updated_at DESC LIMIT 1", (agent_id,)
        )
        return self._row_to_agent(rows[0]) if rows else None

    async def get_agent_by_id(self, agent_id: int) -> AgentStatus | None:
        rows = await self._execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        )
        return self._row_to_agent(rows[0]) if rows else None

    async def list_agents(self) -> list[AgentStatus]:
        # Stable registration order: `updated_at` is bumped on every heartbeat
        # (see upsert_agent), so ordering by it reshuffles the fleet every poll.
        rows = await self._execute("SELECT * FROM agents ORDER BY id ASC")
        return [self._row_to_agent(row) for row in rows]

    async def set_agent_online(self, device_id: str, online: bool) -> None:
        # Match by device_id, or by agent_id when the row has no device_id (a
        # legacy/abnormal node would otherwise never be flipped offline).
        await self._execute(
            "UPDATE agents SET online = ?, updated_at = ? "
            "WHERE device_id = ? OR (device_id IS NULL AND agent_id = ?)",
            (online, _dt_to_iso(datetime.now(timezone.utc)), device_id, device_id),
        )

    async def set_agent_current_task(
        self, device_id: str, task_id: str | None
    ) -> None:
        await self._execute(
            "UPDATE agents SET current_task_id = ?, updated_at = ? "
            "WHERE device_id = ? OR (device_id IS NULL AND agent_id = ?)",
            (task_id, _dt_to_iso(datetime.now(timezone.utc)), device_id, device_id),
        )

    async def set_agent_alias_by_id(self, agent_id: int, alias: str | None) -> None:
        await self._execute(
            "UPDATE agents SET alias = ?, updated_at = ? WHERE id = ?",
            (alias, _dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )

    async def set_agent_description_by_id(
        self, agent_id: int, description: str | None
    ) -> None:
        await self._execute(
            "UPDATE agents SET description = ?, updated_at = ? WHERE id = ?",
            (description, _dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )

    async def set_agent_system_prompt_by_id(
        self, agent_id: int, system_prompt: str | None
    ) -> None:
        await self._execute(
            "UPDATE agents SET system_prompt = ?, updated_at = ? WHERE id = ?",
            (system_prompt, _dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )

    async def set_agent_template_by_id(
        self, agent_id: int, template_id: int | None
    ) -> None:
        await self._execute(
            "UPDATE agents SET template_id = ?, updated_at = ? WHERE id = ?",
            (template_id, _dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )

    async def set_agent_access_by_id(
        self, agent_id: int, access: dict[str, Any] | None
    ) -> None:
        await self._execute(
            "UPDATE agents SET access = ?, updated_at = ? WHERE id = ?",
            (_dump_json(access), _dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )

    async def delete_agent(self, agent_id: int) -> bool:
        affected = await self._execute_rowcount(
            "DELETE FROM agents WHERE id = ?", (agent_id,)
        )
        return affected > 0

    async def set_agent_llm_config(
        self,
        agent_id: int,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        await self._execute(
            "UPDATE agents SET llm_api_key = ?, llm_base_url = ?, llm_model = ?, updated_at = ? WHERE id = ?",
            (llm_api_key, llm_base_url, llm_model, _dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )

    async def get_agent_token_hash(self, agent_id: int) -> str | None:
        rows = await self._execute(
            "SELECT token_hash FROM agents WHERE id = ?", (agent_id,)
        )
        return rows[0]["token_hash"] if rows else None

    async def set_agent_token_hash(self, agent_id: int, token_hash: str | None) -> None:
        await self._execute(
            "UPDATE agents SET token_hash = ?, updated_at = ? WHERE id = ?",
            (token_hash, _dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )

    async def get_agent_by_token_hash(self, token_hash: str) -> AgentStatus | None:
        rows = await self._execute(
            "SELECT * FROM agents WHERE token_hash = ? LIMIT 1", (token_hash,)
        )
        return self._row_to_agent(rows[0]) if rows else None

    async def add_agent_user(self, agent_id: int, user_id: str) -> None:
        await self._execute(
            """
            INSERT INTO agent_users (agent_id, user_id)
            VALUES (?, ?)
            ON CONFLICT(agent_id, user_id) DO NOTHING
            """,
            (agent_id, user_id),
        )

    async def list_agent_users(self, agent_id: int) -> list[str]:
        rows = await self._execute(
            "SELECT user_id FROM agent_users WHERE agent_id = ?", (agent_id,)
        )
        return [row["user_id"] for row in rows]

    async def request_agent_upgrade(self, agent_id: int, version: str) -> bool:
        now = _dt_to_iso(datetime.now(timezone.utc))
        affected = await self._execute_rowcount(
            "UPDATE agents SET upgrade_requested = 1, upgrade_version = ?, upgrade_requested_at = ?, updated_at = ? WHERE id = ?",
            (version, now, now, agent_id),
        )
        return affected > 0

    async def clear_agent_upgrade(self, agent_id: int) -> None:
        await self._execute(
            "UPDATE agents SET upgrade_requested = 0, upgrade_version = NULL, upgrade_requested_at = NULL, updated_at = ? WHERE id = ?",
            (_dt_to_iso(datetime.now(timezone.utc)), agent_id),
        )
