"""Shared helpers and request payloads for the /api/agents/* routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import AgentStatus


class AliasPayload(BaseModel):
    alias: str | None = None


class DescriptionPayload(BaseModel):
    description: str | None = None


class SystemPromptPayload(BaseModel):
    system_prompt: str | None = None


class TemplatePayload(BaseModel):
    template_id: int | None = None


class LlmConfigPayload(BaseModel):
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None


class AccessPayload(BaseModel):
    teams: list[str] | None = None
    users: list[str] | None = None


class BatchAccessPayload(BaseModel):
    agent_ids: list[int] = []
    teams: list[str] | None = None
    users: list[str] | None = None
    mode: str = "add"  # add | remove | set


class BatchTemplatePayload(BaseModel):
    agent_ids: list[int] = []
    template_id: int | None = None


async def resolve_agent(store: TaskStore, agent_id: str) -> AgentStatus | None:
    """Resolve a numeric id, device_id, or agent_id string to an AgentStatus."""
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


async def require_agent(
    store: TaskStore, agent_id: str, user: dict[str, Any]
) -> AgentStatus:
    """Resolve an agent the caller is allowed to see, else 404 (no leak)."""
    agent = await resolve_agent(store, agent_id)
    if agent is None or not await store.can_access_agent(user, agent):
        raise HTTPException(status_code=404, detail="agent not found")
    return agent


def dump_agent(store: TaskStore, agent: AgentStatus, user: dict[str, Any]) -> dict[str, Any]:
    data = agent.model_dump_json_safe()
    if not store.is_admin(user):
        data.pop("access", None)  # don't expose the ACL to non-admins
    return data


async def bind_template_checked(
    store: TaskStore, agent: AgentStatus, template_id: int | None, user: dict[str, Any]
) -> None:
    """Validate ownership/b1-lock and bind a template (raises HTTPException)."""
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

    if template_id is not None:
        template = await store.store.get_template(template_id)
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
    await store.store.set_agent_template_by_id(agent.id, template_id)
    agent.template_id = template_id


async def check_template_owner(
    store: TaskStore, template_id: int, user: dict[str, Any]
) -> dict[str, Any]:
    """Fetch a template and ensure the caller owns it (raises HTTPException)."""
    template = await store.store.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    if not store.is_admin(user):
        team = await store.user_team(user)
        owned = (template.get("owner_user_id") == user.get("user_id")) or bool(
            team and template.get("owner_team_id") == team
        )
        if not owned:
            raise HTTPException(status_code=403, detail="you do not own this template")
    return template
