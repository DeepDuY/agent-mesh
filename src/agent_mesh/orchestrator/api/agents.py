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
) -> None:

    @router.get("/agents")
    async def list_agents(
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agents = await store.list_agents()
        return {"agents": [a.model_dump_json_safe() for a in agents]}

    @router.get("/agents/{agent_id}")
    async def get_agent(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        return {"agent": agent.model_dump_json_safe()}

    @router.get("/agents/{agent_id}/detail")
    async def get_agent_detail(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        key = agent.device_id or agent.agent_id
        tasks = await store.list_tasks(agent_id=key, limit=20)
        detail = AgentDetail(
            **agent.model_dump(),
            tasks=[t.model_dump_json_safe() for t in tasks],
            metadata={
                "allowed_users": await store.store.list_agent_users(agent.id),
            },
        )
        return {"agent": detail.model_dump_json_safe()}

    @router.post("/agents/{agent_id}/token")
    async def rotate_agent_token(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Rotate the agent's independent token (returned exactly once)."""
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
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
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await store.store.set_agent_alias_by_id(agent.id, payload.alias)
        agent.alias = payload.alias
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/description")
    async def patch_agent_description(
        agent_id: str,
        payload: _DescriptionPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        await store.store.set_agent_description_by_id(agent.id, payload.description)
        agent.description = payload.description
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/system_prompt")
    async def patch_agent_system_prompt(
        agent_id: str,
        payload: _SystemPromptPayload,
        user: dict[str, Any] = Depends(require_user_token),
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
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if payload.template_id is not None:
            if await store.store.get_template(payload.template_id) is None:
                raise HTTPException(status_code=404, detail="template not found")
        await store.store.set_agent_template_by_id(agent.id, payload.template_id)
        agent.template_id = payload.template_id
        # Re-resolve the bound node's config (prompt/model may change).
        await store.bump_config_version()
        logger.info(
            "agent %s bound to template %s by %s",
            agent.id, payload.template_id, user["username"],
        )
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/llm_config")
    async def patch_agent_llm_config(
        agent_id: str,
        payload: _LlmConfigPayload,
        user: dict[str, Any] = Depends(require_user_token),
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

    @router.post("/agents/{agent_id}/upgrade")
    async def request_agent_upgrade(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
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
        agent = await _get_agent_by_numeric_or_string_id(store, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")

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
