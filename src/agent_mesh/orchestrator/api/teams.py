from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)


class _TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None


class _TeamPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None


class _MemberPayload(BaseModel):
    user_id: str


class _TeamNodesPayload(BaseModel):
    agent_ids: list[int] = Field(default_factory=list)


def _new_team_id() -> str:
    return f"team-{secrets.token_hex(6)}"


def mount_team_routes(
    router: APIRouter,
    store: TaskStore,
    require_admin,
) -> None:
    """Team/group management (admin-only). A user belongs to at most one team."""

    async def _public(team: dict[str, Any]) -> dict[str, Any]:
        return {
            "team_id": team["team_id"],
            "name": team["name"],
            "description": team.get("description"),
            "members": await store.store.list_team_members(team["team_id"]),
            "created_at": _iso(team.get("created_at")),
            "updated_at": _iso(team.get("updated_at")),
        }

    @router.get("/teams")
    async def list_teams(
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        teams = await store.store.list_teams()
        return {"teams": [await _public(t) for t in teams]}

    @router.post("/teams")
    async def create_team(
        payload: _TeamCreate,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        name = payload.name.strip()
        if await store.store.get_team_by_name(name) is not None:
            raise HTTPException(status_code=409, detail="team name already exists")
        team_id = _new_team_id()
        await store.store.create_team(team_id, name, payload.description)
        team = await store.store.get_team(team_id)
        logger.info("created team %s (%s) by %s", team_id, name, user["username"])
        return {"team": await _public(team)}

    @router.patch("/teams/{team_id}")
    async def patch_team(
        team_id: str,
        payload: _TeamPatch,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        team = await store.store.get_team(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="team not found")
        fields = payload.model_dump(exclude_unset=True)
        if fields.get("name") is not None:
            name = fields["name"].strip()
            existing = await store.store.get_team_by_name(name)
            if existing is not None and existing["team_id"] != team_id:
                raise HTTPException(status_code=409, detail="team name already exists")
            fields["name"] = name
        await store.store.update_team(team_id, **fields)
        return {"team": await _public(await store.store.get_team(team_id))}

    @router.delete("/teams/{team_id}")
    async def delete_team(
        team_id: str,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        deleted = await store.store.delete_team(team_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="team not found")
        logger.info("deleted team %s by %s", team_id, user["username"])
        return {"deleted": True}

    @router.post("/teams/{team_id}/members")
    async def add_member(
        team_id: str,
        payload: _MemberPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        team = await store.store.get_team(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="team not found")
        target = await store.store.get_user_by_username(payload.user_id)
        # user_id accepts either the internal id or the username.
        if target is None:
            target = await _get_user_by_id(store, payload.user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        # A user belongs to at most one team: this moves them if already in one.
        await store.store.set_user_team(target["user_id"], team_id)
        return {"team": await _public(await store.store.get_team(team_id))}

    @router.delete("/teams/{team_id}/members/{user_id}")
    async def remove_member(
        team_id: str,
        user_id: str,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        target = await store.store.get_user_by_username(user_id)
        if target is None:
            target = await _get_user_by_id(store, user_id)
        if target is not None:
            current = await store.store.get_user_team(target["user_id"])
            if current == team_id:
                await store.store.set_user_team(target["user_id"], None)
        return {"team": await _public(await store.store.get_team(team_id))}

    @router.put("/teams/{team_id}/nodes")
    async def set_team_nodes(
        team_id: str,
        payload: _TeamNodesPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        """Replace the set of nodes a team may operate (admin-only).

        The node's ACL lives on the node (``agents.access.teams``); this makes
        the team->nodes direction a first-class operation so an admin does not
        have to open every node to grant a whole team access.
        """
        team = await store.store.get_team(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="team not found")
        desired = set(payload.agent_ids)
        updated = 0
        for agent in await store.store.list_agents():
            access = dict(agent.access or {})
            teams = set(access.get("teams") or [])
            has = team_id in teams
            want = agent.id in desired
            if has == want:
                continue
            if want:
                teams.add(team_id)
            else:
                teams.discard(team_id)
            access["teams"] = sorted(teams)
            await store.store.set_agent_access_by_id(agent.id, access)
            updated += 1
        logger.info(
            "team %s node access set (%d changed) by %s",
            team_id, updated, user["username"],
        )
        return {"updated": updated, "total": len(desired)}


async def _get_user_by_id(store: TaskStore, user_id: str) -> dict[str, Any] | None:
    for u in await store.store.list_users():
        if u.get("user_id") == user_id:
            return u
    return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
