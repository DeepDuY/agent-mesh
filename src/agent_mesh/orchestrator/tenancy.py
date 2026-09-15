"""Tenancy / access control for :class:`~agent_mesh.orchestrator.task_store.TaskStore`.

Extracted from TaskStore so the authorization rules (node ACL, task visibility,
edge identity checks) live together and can be read in one place.
"""

from __future__ import annotations

from typing import Any


class TenancyMixin:
    """Node ACL + task visibility checks. Requires ``self.store``."""

    @staticmethod
    def is_admin(user: dict[str, Any] | None) -> bool:
        return bool(user) and user.get("role") == "admin"

    async def user_team(self, user: dict[str, Any] | None) -> str | None:
        user_id = (user or {}).get("user_id")
        if not user_id:
            return None
        return await self.store.get_user_team(user_id)

    async def can_access_agent(self, user: dict[str, Any] | None, agent) -> bool:
        """Whether ``user`` may operate ``agent`` (admin always may)."""
        if self.is_admin(user):
            return True
        if agent is None:
            return False
        access = agent.access or {}
        user_id = (user or {}).get("user_id")
        if user_id and user_id in (access.get("users") or []):
            return True
        team = await self.user_team(user)
        return bool(team and team in (access.get("teams") or []))

    async def accessible_agent_ids(
        self, user: dict[str, Any] | None
    ) -> set[int] | None:
        """Numeric ids the user may access, or None for admin (all)."""
        if self.is_admin(user):
            return None
        user_id = (user or {}).get("user_id")
        team = await self.user_team(user)
        ids: set[int] = set()
        for a in await self.store.list_agents():
            access = a.access or {}
            if (user_id and user_id in (access.get("users") or [])) or (
                team and team in (access.get("teams") or [])
            ):
                ids.add(a.id)
        return ids

    async def grant_agent_access(
        self,
        agent,
        *,
        user_id: str | None = None,
        team_id: str | None = None,
    ) -> bool:
        """Add a user and/or team to a node's access list. Returns True if changed."""
        access = dict(agent.access or {})
        users = set(access.get("users") or [])
        teams = set(access.get("teams") or [])
        changed = False
        if user_id and user_id not in users:
            users.add(user_id)
            changed = True
        if team_id and team_id not in teams:
            teams.add(team_id)
            changed = True
        if changed:
            access["users"] = sorted(users)
            access["teams"] = sorted(teams)
            await self.store.set_agent_access_by_id(agent.id, access)
            agent.access = access
        return changed

    async def can_see_task(self, user: dict[str, Any] | None, task) -> bool:
        """Whether a user identity may see ``task`` (admin always may).

        Visibility = own task (``user_id``) OR same team (``team_id``).
        """
        if self.is_admin(user):
            return True
        if task is None:
            return False
        user_id = (user or {}).get("user_id")
        if user_id and task.user_id == user_id:
            return True
        team = await self.user_team(user)
        return bool(team and task.team_id == team)

    async def edge_can_access_task(self, auth: dict[str, Any] | None, task) -> bool:
        """Whether an edge identity may read/write ``task``.

        - ``global`` token: trusted (legacy bootstrap).
        - ``user`` token: same tenancy visibility as the REST API, or the user
          is an operator of the node the task runs on (legacy user-token edges).
        - ``agent`` token: the task must be queued on that agent's stable key
          (``device_id`` or ``agent_id``) — an agent may only touch its own tasks.
        """
        kind = (auth or {}).get("auth")
        if kind == "global":
            return True
        if task is None:
            return False
        if kind == "user":
            if await self.can_see_task(auth, task):
                return True
            # Legacy edges may authenticate with a user token (the operator
            # running the node): allow results for tasks on nodes they can operate.
            agent = await self._resolve_agent(task.agent_id)
            return await self.can_access_agent(auth, agent)
        if kind == "agent":
            agent = await self.store.get_agent_by_id(auth.get("agent_id"))
            key = (agent.device_id or agent.agent_id) if agent else None
            return bool(key and task.agent_id == key)
        return False
