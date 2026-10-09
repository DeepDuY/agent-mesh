from __future__ import annotations

from typing import Any

from agent_mesh.orchestrator.store.connection import _utcnow
from agent_mesh.orchestrator.store.pg.base import PostgresBase


class TeamMixin(PostgresBase):
    """Teams / groups and membership (a user belongs to at most one team)."""

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
        # Detach the deleted team from owned resources and node ACLs so it does
        # not linger as a dangling reference (ghost team pointer). The legacy
        # single-team columns are also cleared.
        for table, column in (
            ("tasks", "team_ids"),
            ("schedules", "team_ids"),
            ("templates", "owner_team_ids"),
        ):
            await self._db.execute(
                f"UPDATE {table} SET {column} = COALESCE("
                f"(SELECT jsonb_agg(v) FROM jsonb_array_elements_text({table}.{column}) AS t(v) "
                f"WHERE v <> ?), '[]'::jsonb) "
                f"WHERE jsonb_exists({column}, ?)",
                (team_id, team_id),
            )
        await self._db.execute("UPDATE tasks SET team_id = NULL WHERE team_id = ?", (team_id,))
        await self._db.execute("UPDATE schedules SET team_id = NULL WHERE team_id = ?", (team_id,))
        await self._db.execute(
            "UPDATE templates SET owner_team_id = NULL WHERE owner_team_id = ?", (team_id,)
        )
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

    async def add_user_to_team(self, user_id: str, team_id: str) -> None:
        """Add a user to a team (idempotent; a user may belong to many teams)."""
        await self._db.execute(
            "INSERT INTO team_members (team_id, user_id) VALUES (?, ?) "
            "ON CONFLICT (team_id, user_id) DO NOTHING",
            (team_id, user_id),
        )

    async def remove_user_from_team(self, user_id: str, team_id: str) -> None:
        await self._db.execute(
            "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, user_id),
        )

    async def get_user_teams(self, user_id: str) -> list[str]:
        rows = await self._db.execute(
            "SELECT team_id FROM team_members WHERE user_id = ? ORDER BY team_id",
            (user_id,),
        )
        return [r["team_id"] for r in rows]
