from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import (
    _dt_to_iso,
    SQLiteBase,
)


class TeamMixin(SQLiteBase):
    async def create_team(
        self, team_id: str, name: str, description: str | None = None
    ) -> None:
        await self._execute(
            "INSERT INTO teams (team_id, name, description) VALUES (?, ?, ?)",
            (team_id, name, description),
        )

    async def get_team(self, team_id: str) -> dict[str, Any] | None:
        rows = await self._execute(
            "SELECT team_id, name, description, created_at, updated_at "
            "FROM teams WHERE team_id = ?",
            (team_id,),
        )
        return dict(rows[0]) if rows else None

    async def get_team_by_name(self, name: str) -> dict[str, Any] | None:
        rows = await self._execute(
            "SELECT team_id, name, description, created_at, updated_at "
            "FROM teams WHERE name = ?",
            (name,),
        )
        return dict(rows[0]) if rows else None

    async def list_teams(self) -> list[dict[str, Any]]:
        rows = await self._execute(
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
        params.append(_dt_to_iso(datetime.now(timezone.utc)))
        params.append(team_id)
        count = await self._execute_rowcount(
            f"UPDATE teams SET {', '.join(sets)} WHERE team_id = ?", tuple(params)
        )
        return count > 0

    async def delete_team(self, team_id: str) -> bool:
        await self._execute("DELETE FROM team_members WHERE team_id = ?", (team_id,))
        # Detach the deleted team from tasks and node ACLs so it does not linger
        # as a dangling reference (ghost team pointer).
        await self._execute("UPDATE tasks SET team_id = NULL WHERE team_id = ?", (team_id,))
        for agent in await self.list_agents():
            access = dict(agent.access or {})
            teams = access.get("teams") or []
            remaining = [t for t in teams if t != team_id]
            if remaining != teams:
                access["teams"] = remaining
                await self.set_agent_access_by_id(agent.id, access)
        count = await self._execute_rowcount(
            "DELETE FROM teams WHERE team_id = ?", (team_id,)
        )
        return count > 0

    async def list_team_members(self, team_id: str) -> list[str]:
        rows = await self._execute(
            "SELECT user_id FROM team_members WHERE team_id = ? ORDER BY user_id",
            (team_id,),
        )
        return [r["user_id"] for r in rows]

    async def set_user_team(self, user_id: str, team_id: str | None) -> None:
        """Assign a user to a team (at most one). Pass ``None`` to remove."""
        await self._execute("DELETE FROM team_members WHERE user_id = ?", (user_id,))
        if team_id:
            await self._execute(
                "INSERT INTO team_members (team_id, user_id) VALUES (?, ?)",
                (team_id, user_id),
            )

    async def get_user_team(self, user_id: str) -> str | None:
        rows = await self._execute(
            "SELECT team_id FROM team_members WHERE user_id = ?", (user_id,)
        )
        return rows[0]["team_id"] if rows else None
