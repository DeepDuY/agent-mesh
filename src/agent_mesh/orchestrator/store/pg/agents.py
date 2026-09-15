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
from agent_mesh.shared.schemas import (
    METRIC_FIELDS,
    SYSTEM_FIELDS,
    TELEMETRY_FIELDS,
    AgentStatus,
    validate_telemetry,
)


class AgentMixin(PostgresBase):
    """Agents table: registration, status, tokens, ACL, upgrade flags."""

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
            ip_address=row.get("ip_address"),
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
        ip_address: str | None = None,
    ) -> int:
        telemetry = dict(telemetry or {})
        validate_telemetry(telemetry)
        now = _utcnow()
        existing = await self.get_agent(device_id) if device_id else await self.get_agent_by_agent_id(agent_id)
        if existing:
            sets = [
                "agent_id=?", "runtime=?", "hostname=?", "online=?",
                "last_seen_at=?", "current_task_id=?", "metadata=?",
                "version=?", "ip_address=COALESCE(?, ip_address)", "updated_at=?",
            ]
            params: list[Any] = [
                agent_id, runtime, hostname, online,
                last_seen, current_task_id,
                _dump_json(metadata), version, ip_address, now,
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
            "version", "ip_address", "updated_at",
        ]
        vals: list[Any] = [
            agent_id, device_id, runtime, hostname, online,
            last_seen, current_task_id,
            _dump_json(metadata), version, ip_address, now,
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
