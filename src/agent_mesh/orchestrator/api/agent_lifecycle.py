"""Node lifecycle routes: token rotation, upgrade request, deletion."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from agent_mesh.orchestrator.api.agent_common import require_agent
from agent_mesh.orchestrator.auth import generate_token, hash_token
from agent_mesh.orchestrator.config import OrchestratorConfig, bootstrap_version
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import Constraints

logger = logging.getLogger(__name__)


def mount_agent_lifecycle_routes(
    router: APIRouter,
    store: TaskStore,
    config: OrchestratorConfig | None,
    require_user_token,
    require_admin=None,
) -> None:
    # Upgrading and deleting a node are administrative operations: normal users
    # may view/operate nodes they have access to, but must not upgrade or remove
    # them. Fall back to the user guard if no admin dependency was supplied.
    require_admin = require_admin or require_user_token

    @router.post("/agents/{agent_id}/token")
    async def rotate_agent_token(
        agent_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Rotate the agent's independent token (returned exactly once)."""
        agent = await require_agent(store, agent_id, user)
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

    @router.post("/agents/{agent_id}/upgrade")
    async def request_agent_upgrade(
        agent_id: str,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)
        version = bootstrap_version(config.db_path)
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
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        agent = await require_agent(store, agent_id, user)

        key = agent.device_id or agent.agent_id
        if (agent.os or "").lower() == "win32":
            # Remove the Scheduled Task + its launcher. The install directory is
            # left in place: the running agent image is locked on Windows and
            # cannot delete itself mid-command (an admin removes it after).
            instruction = (
                "schtasks /End /TN agent-mesh-edge & "
                "schtasks /Delete /TN agent-mesh-edge /F & "
                "echo agent-mesh uninstalled"
            )
            workdir = "."
        else:
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
            workdir = "/tmp"
        try:
            await store.dispatch(
                agent_id=key,
                instruction=instruction,
                mode="command",
                constraints=Constraints(timeout_s=30, workdir=workdir),
                max_retries=0,
                dispatched_by=user["username"] if user else None,
            )
        except ValueError as e:
            logger.warning("delete agent=%s: dispatch failed (%s); removing record anyway", key, e)

        await store.store.delete_agent(agent.id)
        logger.info("uninstalled agent id=%s (%s)", agent.id, key)
        return {"deleted": True}
