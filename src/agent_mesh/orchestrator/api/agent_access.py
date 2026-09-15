"""Node ACL routes (who may operate a node)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from agent_mesh.orchestrator.api.agent_common import (
    AccessPayload,
    BatchAccessPayload,
    dump_agent,
    require_agent,
)
from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)


def mount_agent_access_routes(
    router: APIRouter,
    store: TaskStore,
    require_admin,
) -> None:
    @router.patch("/agents/{agent_id}/access")
    async def patch_agent_access(
        agent_id: str,
        payload: AccessPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        """Set which teams/users may operate the node (admin-only)."""
        agent = await require_agent(store, agent_id, user)
        access = {
            "teams": sorted(set(payload.teams or [])),
            "users": sorted(set(payload.users or [])),
        }
        await store.store.set_agent_access_by_id(agent.id, access)
        agent.access = access
        logger.info("updated access for agent %s by %s", agent.id, user["username"])
        return {"agent": dump_agent(store, agent, user)}

    @router.post("/agents/batch/access")
    async def batch_set_agent_access(
        payload: BatchAccessPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        """Add/remove/replace teams+users on many nodes at once (admin-only)."""
        if payload.mode not in ("add", "remove", "set"):
            raise HTTPException(status_code=400, detail="mode must be add, remove or set")
        add_teams = set(payload.teams or [])
        add_users = set(payload.users or [])
        updated = 0
        for agent_id in payload.agent_ids:
            agent = await store.store.get_agent_by_id(agent_id)
            if agent is None:
                continue
            access = dict(agent.access or {})
            teams = set(access.get("teams") or [])
            users = set(access.get("users") or [])
            if payload.mode == "set":
                new_teams, new_users = set(add_teams), set(add_users)
            elif payload.mode == "add":
                new_teams, new_users = teams | add_teams, users | add_users
            else:  # remove
                new_teams, new_users = teams - add_teams, users - add_users
            new_access = {"teams": sorted(new_teams), "users": sorted(new_users)}
            if new_access != access:
                await store.store.set_agent_access_by_id(agent.id, new_access)
                updated += 1
        logger.info(
            "batch access %s updated %d nodes by %s",
            payload.mode, updated, user["username"],
        )
        return {"updated": updated}
