from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import (
    _dt_to_iso,
    _dump_json,
    _load_json,
    SQLiteBase,
)

_TEMPLATE_COLUMNS = (
    "id, name, description, system_prompt, llm_model, allowed_tools, data, "
    "created_at, updated_at"
)


def _row_to_template(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["allowed_tools"] = _load_json(data.get("allowed_tools"))
    data["data"] = _load_json(data.get("data"))
    return data


class TemplateMixin(SQLiteBase):
    async def create_template(
        self,
        name: str,
        description: str | None = None,
        system_prompt: str | None = None,
        llm_model: str | None = None,
        allowed_tools: list[str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        rows = await self._execute(
            """
            INSERT INTO templates
                (name, description, system_prompt, llm_model, allowed_tools, data)
            VALUES (?, ?, ?, ?, ?, ?) RETURNING id
            """,
            (
                name,
                description,
                system_prompt,
                llm_model,
                _dump_json(allowed_tools),
                _dump_json(data),
            ),
        )
        return rows[0]["id"]

    async def get_template(self, template_id: int) -> dict[str, Any] | None:
        rows = await self._execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates WHERE id = ?", (template_id,)
        )
        return _row_to_template(rows[0]) if rows else None

    async def get_template_by_name(self, name: str) -> dict[str, Any] | None:
        rows = await self._execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates WHERE name = ?", (name,)
        )
        return _row_to_template(rows[0]) if rows else None

    async def list_templates(self) -> list[dict[str, Any]]:
        rows = await self._execute(
            f"SELECT {_TEMPLATE_COLUMNS} FROM templates ORDER BY name ASC"
        )
        return [_row_to_template(row) for row in rows]

    async def update_template(self, template_id: int, **fields: Any) -> bool:
        allowed = {
            "name",
            "description",
            "system_prompt",
            "llm_model",
            "allowed_tools",
            "data",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("allowed_tools", "data"):
                value = _dump_json(value)
            sets.append(f"{key}=?")
            params.append(value)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_dt_to_iso(datetime.now(timezone.utc)))
        params.append(template_id)
        count = await self._execute_rowcount(
            f"UPDATE templates SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        return count > 0

    async def delete_template(self, template_id: int) -> bool:
        count = await self._execute_rowcount(
            "DELETE FROM templates WHERE id = ?", (template_id,)
        )
        return count > 0
