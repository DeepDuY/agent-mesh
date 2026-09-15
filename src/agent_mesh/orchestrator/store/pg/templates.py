from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.connection import _dump_json, _load_json, _utcnow
from agent_mesh.orchestrator.store.pg.base import PostgresBase


def _row_to_template(row: dict[str, Any]) -> dict[str, Any]:
    row["permission"] = _load_json(row.get("permission"))
    row["data"] = _load_json(row.get("data"))
    return row


class TemplateMixin(PostgresBase):
    """Node templates (reusable node config + permission)."""

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
