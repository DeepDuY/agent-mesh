"""LLM-config and execution-permission resolution for TaskStore.

Extracted from TaskStore: model allow-list resolution, the effective (template >
global default > built-in readonly) permission, command pre-checks, agent token
issuance and config-version bumping.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from agent_mesh.shared.schemas import AgentStatus

logger = logging.getLogger(__name__)


class PermissionMixin:
    """Edge config + permission resolution. Requires ``self.store``."""

    async def ensure_agent_token(self, agent_id: int) -> str | None:
        """Issue a per-agent independent token on first registration.

        Returns the plaintext token exactly once (only when it is newly
        generated); subsequent calls return None. The hash is stored in the DB,
        so the agent token does not depend on any user login session.
        """
        existing = await self.store.get_agent_token_hash(agent_id)
        if existing:
            return None
        from agent_mesh.orchestrator.auth import generate_token, hash_token

        token = generate_token()
        await self.store.set_agent_token_hash(agent_id, hash_token(token))
        logger.info("issued new independent token for agent id=%s", agent_id)
        return token

    async def bump_config_version(self) -> None:
        """Increment the global LLM config version so edges re-sync their config."""
        current = await self.store.get_setting("config_version")
        try:
            version = int(current or 0)
        except ValueError:
            version = 0
        await self.store.set_setting("config_version", str(version + 1))

    async def allowed_models(self) -> list[str]:
        """Configured model allow-list (empty = no restriction)."""
        raw = await self.store.get_setting("llm_models") or ""
        return [m.strip() for m in re.split(r"[,\n]", raw) if m.strip()]

    async def resolve_llm_model(
        self, agent: AgentStatus | None, explicit: str | None
    ) -> str:
        """Effective model: explicit > node > bound template > global default."""
        if explicit:
            return explicit
        if agent is not None:
            if agent.llm_model:
                return agent.llm_model
            if agent.template_id:
                template = await self.store.get_template(agent.template_id)
                if template and template.get("llm_model"):
                    return template["llm_model"]
        return (await self.store.get_setting("llm_model")) or ""

    async def validate_llm_model(
        self, agent_ref: str, mode: str, explicit: str | None
    ) -> str | None:
        """Return an error message when an llm dispatch cannot proceed, else None.

        Enforces the force-configured policy (no hardcoded fallback model) and,
        when an allow-list is configured, that an explicit `model` is on it.
        """
        if mode != "llm":
            return None
        agent = await self._resolve_agent(agent_ref)
        resolved = await self.resolve_llm_model(agent, explicit)
        if not resolved:
            return "no LLM model configured: pass model or set a default model"
        allowed = await self.allowed_models()
        if explicit and allowed and explicit not in allowed:
            return f"model not in allowed list: {explicit}"
        return None

    async def effective_permission(
        self, agent: AgentStatus | None, template: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Resolve the permission object governing a node.

        Permission is a template-level capability (no node/task override):
        bound template > global ``default_permission`` setting > built-in readonly.
        """
        from agent_mesh.shared import permissions

        if template is not None and template.get("permission"):
            return template["permission"]
        permission = permissions.loads(
            await self.store.get_setting("default_permission")
        )
        return permission if permission is not None else permissions.default_permission()

    async def check_command_permission(
        self, agent_ref: str, instruction: str
    ) -> str | None:
        """Return a denial reason when a command task is not permitted, else None.

        Server-side pre-check for immediate feedback; the edge re-evaluates as the
        enforcement point. Returns None when the agent is unknown (the normal
        "agent not found" path handles that).
        """
        from agent_mesh.shared import permissions

        agent = await self._resolve_agent(agent_ref)
        if agent is None:
            return None
        template = (
            await self.store.get_template(agent.template_id)
            if agent.template_id
            else None
        )
        permission = await self.effective_permission(agent, template)
        if permissions.evaluate_command(permission, instruction) != "allow":
            return "command denied by permission policy"
        return None
