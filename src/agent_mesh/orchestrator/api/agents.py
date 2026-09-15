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

from fastapi import APIRouter, Depends

from agent_mesh.orchestrator.api.agent_access import mount_agent_access_routes
from agent_mesh.orchestrator.api.agent_common import (
    AliasPayload,
    DescriptionPayload,
    dump_agent,
    require_agent,
)
from agent_mesh.orchestrator.api.agent_config import mount_agent_config_routes
from agent_mesh.orchestrator.api.agent_lifecycle import mount_agent_lifecycle_routes
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import AgentDetail

logger = logging.getLogger(__name__)


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
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await store.describe_agents([agent])
        return {"agent": dump_agent(store, agent, user)}

    @router.get("/agents/{agent_id}/detail")
    async def get_agent_detail(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await store.describe_agents([agent])
        key = agent.device_id or agent.agent_id
        tasks = await store.list_tasks(agent_id=key, limit=20)
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
            data.pop("access", None)
        return {"agent": data}

    @router.patch("/agents/{agent_id}/alias")
    async def patch_agent_alias(
        agent_id: str,
        payload: AliasPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await store.store.set_agent_alias_by_id(agent.id, payload.alias)
        agent.alias = payload.alias
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/description")
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
    mount_agent_lifecycle_routes(router, store, config, require_user_token)
