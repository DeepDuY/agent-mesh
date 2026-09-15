"""Node configuration routes: system prompt, LLM config, template binding."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from agent_mesh.orchestrator.api.agent_common import (
    BatchTemplatePayload,
    LlmConfigPayload,
    SystemPromptPayload,
    TemplatePayload,
    bind_template_checked,
    check_template_owner,
    require_agent,
)
from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)


def mount_agent_config_routes(
    router: APIRouter,
    store: TaskStore,
    require_admin,
    require_ui_user,
) -> None:
    @router.patch("/agents/{agent_id}/system_prompt")
    async def patch_agent_system_prompt(
        agent_id: str,
        payload: SystemPromptPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await store.store.set_agent_system_prompt_by_id(
            agent.id, payload.system_prompt
        )
        agent.system_prompt = payload.system_prompt
        # Push the change to the node via the config-sync channel.
        await store.bump_config_version()
        return {"agent": agent.model_dump_json_safe()}

    @router.patch("/agents/{agent_id}/llm_config")
    async def patch_agent_llm_config(
        agent_id: str,
        payload: LlmConfigPayload,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
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

    @router.patch("/agents/{agent_id}/template")
    async def patch_agent_template(
        agent_id: str,
        payload: TemplatePayload,
        user: dict[str, Any] = Depends(require_ui_user),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        await bind_template_checked(store, agent, payload.template_id, user)
        # Re-resolve the bound node's config (prompt/model may change).
        await store.bump_config_version()
        await store.describe_agents([agent])
        logger.info(
            "agent %s bound to template %s by %s",
            agent.id, payload.template_id, user["username"],
        )
        return {"agent": agent.model_dump_json_safe()}

    @router.post("/agents/batch/template")
    async def batch_apply_template(
        payload: BatchTemplatePayload,
        user: dict[str, Any] = Depends(require_ui_user),
    ) -> dict[str, Any]:
        """Bind one template to many nodes at once (skips inaccessible/locked)."""
        if not payload.agent_ids:
            return {"applied": 0, "skipped": []}

        # Validate the template once so a common failure is a clean 4xx.
        if payload.template_id is not None:
            await check_template_owner(store, payload.template_id, user)

        applied = 0
        skipped: list[int] = []
        for agent_id in payload.agent_ids:
            agent = await store.store.get_agent_by_id(agent_id)
            if agent is None or not await store.can_access_agent(user, agent):
                skipped.append(agent_id)
                continue
            try:
                await bind_template_checked(store, agent, payload.template_id, user)
                applied += 1
            except HTTPException:
                skipped.append(agent_id)
        if applied:
            await store.bump_config_version()
        logger.info(
            "batch bound template %s to %d nodes by %s",
            payload.template_id, applied, user["username"],
        )
        return {"applied": applied, "skipped": skipped}
