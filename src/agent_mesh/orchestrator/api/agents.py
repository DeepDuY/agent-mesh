"""Node routes: read/status endpoints + mounting of the config/access/lifecycle groups.

The routes are split by concern to keep each module small:

* :mod:`agent_config`    -- system prompt, LLM config, template binding
* :mod:`agent_access`    -- ACL (single + batch)
* :mod:`agent_lifecycle` -- token rotation, upgrade, deletion
* :mod:`agent_common`    -- shared payloads + helpers
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_mesh.orchestrator.api.agent_access import mount_agent_access_routes
from agent_mesh.orchestrator.api.agent_common import (
    AliasPayload,
    DescriptionPayload,
    bind_template_checked,
    check_template_owner,
    dump_agent,
    require_agent,
)
from agent_mesh.orchestrator.api.agent_config import mount_agent_config_routes
from agent_mesh.orchestrator.api.agent_lifecycle import mount_agent_lifecycle_routes
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import AgentDetail

logger = logging.getLogger(__name__)


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    template_id: int | None = None
    access: dict[str, list[str]] | None = None


class AgentBatchUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_ids: list[int] = Field(default_factory=list)
    changes: AgentPatch
    mode: str = "set"


def mount_agent_routes(
    router: APIRouter,
    store: TaskStore,
    require_user_token,
    config: OrchestratorConfig | None = None,
    require_admin=None,
    require_ui_user=None,
) -> None:
    # Sensitive node-configuration endpoints (template binding, LLM credentials)
    # must be admin-only; fall back to the user guard if no admin dependency was
    # supplied (keeps direct callers/tests working).
    require_admin = require_admin or require_user_token
    # Template binding is management-only: reachable solely from the Web UI.
    require_ui_user = require_ui_user or require_admin

    @router.get("/agents")
    async def list_agents(
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agents = await store.list_agents()
        allowed = await store.accessible_agent_ids(user)
        if allowed is not None:
            agents = [a for a in agents if a.id in allowed]
        await store.describe_agents(agents)
        return {"agents": [dump_agent(store, a, user) for a in agents]}

    @router.get("/agents/{agent_id}")
    async def get_agent(
        agent_id: str,
        include: str | None = None,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        if include is not None:
            if include != "tasks":
                raise HTTPException(status_code=400, detail="invalid include")
            return await get_agent_detail(agent_id, user)
        agent = await require_agent(store, agent_id, user)
        await store.describe_agents([agent])
        return {"agent": dump_agent(store, agent, user)}

    @router.get("/agents/{agent_id}/detail", include_in_schema=False)
    async def get_agent_detail(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await store.describe_agents([agent])
        key = agent.device_id or agent.agent_id
        owner_user_id = None if store.is_admin(user) else user.get("user_id")
        owner_team_ids = None if store.is_admin(user) else sorted(await store.user_teams(user))
        tasks = await store.list_tasks(
            agent_id=key, limit=20,
            owner_user_id=owner_user_id, owner_team_ids=owner_team_ids,
        )
        detail = AgentDetail(
            **agent.model_dump(),
            tasks=[t.model_dump_json_safe() for t in tasks],
            metadata={
                "allowed_users": (
                    await store.store.list_agent_users(agent.id)
                    if store.is_admin(user)
                    else []
                ),
            },
        )
        data = detail.model_dump_json_safe()
        if not store.is_admin(user):
            for key in ("access", "llm_api_key", "llm_base_url", "llm_model"):
                data.pop(key, None)
        return {"agent": data}

    async def _patch_agent(agent_id: str, payload: AgentPatch, user: dict[str, Any], request: Request):
        agent = await require_agent(store, agent_id, user)
        fields = payload.model_fields_set
        if not fields:
            return {"agent": dump_agent(store, agent, user)}
        sensitive = {"system_prompt", "llm_api_key", "llm_base_url", "llm_model", "access"}
        if fields & sensitive and not store.is_admin(user):
            raise HTTPException(status_code=403, detail="admin role required")
        if "template_id" in fields and request.headers.get("x-agent-mesh-ui") != "1":
            raise HTTPException(status_code=404, detail="Not Found")
        if "access" in fields and payload.access is not None and set(payload.access) - {"users", "teams"}:
            raise HTTPException(status_code=400, detail="invalid access fields")
        if "template_id" in fields:
            if payload.template_id is not None:
                await check_template_owner(store, payload.template_id, user)
            # Check the lock before any other requested fields are modified.
            if not store.is_admin(user) and agent.template_id is not None:
                current = await store.store.get_template(agent.template_id)
                if current and not current.get("owner_user_id") and not current.get("owner_team_ids"):
                    raise HTTPException(status_code=403, detail="this node's template is locked by an administrator")
        if "alias" in fields:
            await store.store.set_agent_alias_by_id(agent.id, payload.alias)
            agent.alias = payload.alias
        if "description" in fields:
            await store.store.set_agent_description_by_id(agent.id, payload.description)
            agent.description = payload.description
        config_changed = False
        if "system_prompt" in fields:
            await store.store.set_agent_system_prompt_by_id(agent.id, payload.system_prompt)
            agent.system_prompt = payload.system_prompt
            config_changed = True
        llm_fields = {"llm_api_key", "llm_base_url", "llm_model"}
        if fields & llm_fields:
            for name in llm_fields & fields:
                setattr(agent, name, getattr(payload, name))
            await store.store.set_agent_llm_config(
                agent.id, agent.llm_api_key, agent.llm_base_url, agent.llm_model
            )
            config_changed = True
        if "template_id" in fields:
            await bind_template_checked(store, agent, payload.template_id, user)
            config_changed = True
        if "access" in fields:
            access = payload.access or {}
            agent.access = {
                "teams": sorted(set(access.get("teams") or [])),
                "users": sorted(set(access.get("users") or [])),
            }
            await store.store.set_agent_access_by_id(agent.id, agent.access)
        if config_changed:
            await store.bump_config_version()
        await store.describe_agents([agent])
        return {"agent": dump_agent(store, agent, user)}

    @router.patch("/agents/{agent_id}")
    async def patch_agent(
        agent_id: str,
        payload: AgentPatch,
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        return await _patch_agent(agent_id, payload, user, request)

    @router.post("/agents:batch_update")
    async def batch_update_agents(
        payload: AgentBatchUpdate,
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        if payload.mode not in {"set", "add", "remove"}:
            raise HTTPException(status_code=400, detail="invalid mode")
        if payload.mode != "set" and payload.changes.model_fields_set != {"access"}:
            raise HTTPException(status_code=400, detail="add/remove require access only")
        if payload.changes.model_fields_set & {"system_prompt", "llm_api_key", "llm_base_url", "llm_model", "access"} and not store.is_admin(user):
            raise HTTPException(status_code=403, detail="admin role required")
        if "template_id" in payload.changes.model_fields_set and request.headers.get("x-agent-mesh-ui") != "1":
            raise HTTPException(status_code=404, detail="Not Found")
        applied, skipped = 0, []
        for agent_id in dict.fromkeys(payload.agent_ids):
            changes = payload.changes
            if payload.mode != "set":
                agent = await store.store.get_agent_by_id(agent_id)
                if agent is None:
                    skipped.append(agent_id)
                    continue
                access = agent.access or {}
                delta = changes.access or {}
                current = {k: set(access.get(k) or []) for k in ("users", "teams")}
                for key in current:
                    values = set(delta.get(key) or [])
                    current[key] = current[key] | values if payload.mode == "add" else current[key] - values
                changes = AgentPatch(access={k: sorted(v) for k, v in current.items()})
            try:
                await _patch_agent(str(agent_id), changes, user, request)
                applied += 1
            except HTTPException as exc:
                if exc.status_code in (403, 404):
                    skipped.append(agent_id)
                else:
                    raise
        return {"applied": applied, "skipped": skipped}

    @router.patch("/agents/{agent_id}/alias", include_in_schema=False)
    async def patch_agent_alias(
        agent_id: str,
        payload: AliasPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await store.store.set_agent_alias_by_id(agent.id, payload.alias)
        agent.alias = payload.alias
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/description", include_in_schema=False)
    async def patch_agent_description(
        agent_id: str,
        payload: DescriptionPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await store.store.set_agent_description_by_id(agent.id, payload.description)
        agent.description = payload.description
        return {"agent": agent.model_dump_json_safe()}

    mount_agent_config_routes(router, store, require_admin, require_ui_user)
    mount_agent_access_routes(router, store, require_admin)
    mount_agent_lifecycle_routes(router, store, config, require_user_token, require_admin)
