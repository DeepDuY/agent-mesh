from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from agent_mesh.orchestrator.auth import generate_token, hash_token
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import AgentDetail, Constraints

logger = logging.getLogger(__name__)


class _AliasPayload(BaseModel):
    alias: str | None = None


class _DescriptionPayload(BaseModel):
    description: str | None = None


class _SystemPromptPayload(BaseModel):
    system_prompt: str | None = None


class _TemplatePayload(BaseModel):
    template_id: int | None = None


class _LlmConfigPayload(BaseModel):
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None


class _AccessPayload(BaseModel):
    teams: list[str] | None = None
    users: list[str] | None = None


def _current_bootstrap_version(config: OrchestratorConfig) -> str | None:
    version_path = Path(config.db_path).parent / "bootstrap" / "VERSION"
    try:
        value = version_path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def mount_agent_routes(
    router: APIRouter,
    store: TaskStore,
    require_user_token,
    config: OrchestratorConfig | None = None,
    require_admin=None,
    require_ui_admin=None,
    require_ui_user=None,
) -> None:
    # Sensitive node-configuration endpoints (template binding, LLM credentials)
    # must be admin-only; fall back to the user guard if no admin dependency was
    # supplied (keeps direct callers/tests working).
    require_admin = require_admin or require_user_token
    # Template binding is management-only: reachable solely from the Web UI.
    require_ui_admin = require_ui_admin or require_admin
    require_ui_user = require_ui_user or require_admin

    async def _require_agent(agent_id: str, user: dict[str, Any]):
        """Resolve an agent the caller is allowed to see, else 404 (no leak)."""
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None or not await store.can_access_agent(user, agent):
            raise HTTPException(status_code=404, detail="agent not found")
        return agent

    def _dump_agent(agent, user: dict[str, Any]) -> dict[str, Any]:
        data = agent.model_dump_json_safe()
        if not store.is_admin(user):
            data.pop("access", None)  # don't expose the ACL to non-admins
        return data

    @router.get("/agents")
    async def list_agents(
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agents = await store.list_agents()
        allowed = await store.accessible_agent_ids(user)
        if allowed is not None:
            agents = [a for a in agents if a.id in allowed]
        await store.describe_agents(agents)
        return {"agents": [_dump_agent(a, user) for a in agents]}

    @router.get("/agents/{agent_id}")
    async def get_agent(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _require_agent(agent_id, user)
        await store.describe_agents([agent])
        return {"agent": _dump_agent(agent, user)}

    @router.get("/agents/{agent_id}/detail")
    async def get_agent_detail(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _require_agent(agent_id, user)
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

    @router.post("/agents/{agent_id}/token")
    async def rotate_agent_token(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Rotate the agent's independent token (returned exactly once)."""
        agent = await _require_agent(agent_id, user)
        token = generate_token()
        await store.store.set_agent_token_hash(agent.id, hash_token(token))
        logger.info(
            "rotated token for agent %s by %s", agent.id, user["username"]
        )
        return {
            "agent_id": agent.id,
            "token": token,
            "token_type": "agent",
        }

    @router.patch("/agents/{agent_id}/alias")
    async def patch_agent_alias(
        agent_id: str,
        payload: _AliasPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _require_agent(agent_id, user)
        await store.store.set_agent_alias_by_id(agent.id, payload.alias)
        agent.alias = payload.alias
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/description")
    async def patch_agent_description(
        agent_id: str,
        payload: _DescriptionPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _require_agent(agent_id, user)
        await store.store.set_agent_description_by_id(agent.id, payload.description)
        agent.description = payload.description
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/system_prompt")
    async def patch_agent_system_prompt(
        agent_id: str,
        payload: _SystemPromptPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await store.store.set_agent_system_prompt_by_id(
            agent.id, payload.system_prompt
        )
        agent.system_prompt = payload.system_prompt
        # Push the change to the node via the config-sync channel.
        await store.bump_config_version()
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/template")
    async def patch_agent_template(
        agent_id: str,
        payload: _TemplatePayload,
        user: dict[str, Any] = Depends(require_ui_user),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None or not await store.can_access_agent(user, agent):
            raise HTTPException(status_code=404, detail="agent not found")

        is_admin = store.is_admin(user)
        team = await store.user_team(user)

        # b1 lock: a non-admin may not override a template binding that was set
        # by an administrator (global templates have no owner).
        if not is_admin and agent.template_id is not None:
            current = await store.store.get_template(agent.template_id)
            if current is not None and not current.get("owner_user_id") and not current.get("owner_team_id"):
                raise HTTPException(
                    status_code=403,
                    detail="this node's template is locked by an administrator",
                )

        if payload.template_id is not None:
            template = await store.store.get_template(payload.template_id)
            if template is None:
                raise HTTPException(status_code=404, detail="template not found")
            if not is_admin:
                owned = (template.get("owner_user_id") == user.get("user_id")) or bool(
                    team and template.get("owner_team_id") == team
                )
                if not owned:
                    raise HTTPException(
                        status_code=403, detail="you do not own this template"
                    )
        await store.store.set_agent_template_by_id(agent.id, payload.template_id)
        agent.template_id = payload.template_id
        # Re-resolve the bound node's config (prompt/model may change).
        await store.bump_config_version()
        await store.describe_agents([agent])
        logger.info(
            "agent %s bound to template %s by %s",
            agent.id, payload.template_id, user["username"],
        )
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/llm_config")
    async def patch_agent_llm_config(
        agent_id: str,
        payload: _LlmConfigPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await store.store.set_agent_llm_config(
            agent.id,
            llm_api_key=payload.llm_api_key,
            llm_base_url=payload.llm_base_url,
            llm_model=payload.llm_model,
        )
        agent.llm_api_key = payload.llm_api_key
        agent.llm_base_url = payload.llm_base_url
        agent.llm_model = payload.llm_model
        # Push the change to the node via the config-sync channel.
        await store.bump_config_version()
        logger.info("updated llm_config for agent %s", agent.id)
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/access")
    async def patch_agent_access(
        agent_id: str,
        payload: _AccessPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        """Set which teams/users may operate the node (admin-only)."""
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        access = {
            "teams": sorted(set(payload.teams or [])),
            "users": sorted(set(payload.users or [])),
        }
        await store.store.set_agent_access_by_id(agent.id, access)
        agent.access = access
        logger.info("updated access for agent %s by %s", agent.id, user["username"])
        return {"agent": _dump_agent(agent, user)}

    @router.post("/agents/{agent_id}/upgrade")
    async def request_agent_upgrade(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _require_agent(agent_id, user)
        version = _current_bootstrap_version(config)
        if not version:
            raise HTTPException(
                status_code=409,
                detail="no bootstrap package available (data/bootstrap/VERSION missing)",
            )
        if agent.upgrade_requested:
            return {"requested": False, "error": "upgrade already requested", "version": agent.upgrade_version}
        requested = await store.store.request_agent_upgrade(agent.id, version)
        if not requested:
            raise HTTPException(status_code=404, detail="agent not found")
        logger.info("requested upgrade of agent %s to %s by %s", agent.id, version, user["username"])
        return {"requested": True, "agent_id": agent.id, "version": version}

    @router.delete("/agents/{agent_id}")
    async def delete_agent(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _require_agent(agent_id, user)

        key = agent.device_id or agent.agent_id
        instruction = (
            "set +e\n"
            "systemctl disable agent-mesh-edge 2>/dev/null\n"
            "rm -f /etc/systemd/system/agent-mesh-edge.service\n"
            "systemctl daemon-reload 2>/dev/null\n"
            'rm -rf "${EDGE_INSTALL_DIR:-/opt/agent-mesh-agent}"\n'
            "echo 'agent-mesh uninstalled'\n"
            "systemctl stop agent-mesh-edge 2>/dev/null\n"
            "true"
        )
        try:
            await store.dispatch(
                agent_id=key,
                instruction=instruction,
                mode="command",
                constraints=Constraints(timeout_s=30, workdir="/tmp"),
                max_retries=0,
                dispatched_by=user["username"] if user else None,
            )
        except ValueError as e:
            logger.warning("delete agent=%s: dispatch failed (%s); removing record anyway", key, e)

        await store.store.delete_agent(agent.id)
        logger.info("uninstalled agent id=%s (%s)", agent.id, key)
        return {"deleted": True}


async def _get_agent_by_numeric_or_string_id(store: TaskStore, agent_id: str):
    from agent_mesh.shared.schemas import AgentStatus

    try:
        numeric_id = int(agent_id)
        agent = await store.store.get_agent_by_id(numeric_id)
        if agent:
            return agent
    except ValueError:
        pass
    agent = await store.store.get_agent(agent_id)
    if agent:
        return agent
    return await store.store.get_agent_by_agent_id(agent_id)
