from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agent_mesh.orchestrator.artifact_store import ArtifactStore
from agent_mesh.orchestrator.auth import hash_token, resolve_token_user
from agent_mesh.orchestrator.config import OrchestratorConfig, constant_time_compare
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import ArtifactRef

security = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


def create_query_router(
    config: OrchestratorConfig,
    store: TaskStore,
    artifact_store: ArtifactStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")
    artifact_store = artifact_store or ArtifactStore(
        base_dir=config.artifact_dir,
        max_size_mb=config.artifact_max_size_mb,
        max_total_mb=config.artifact_max_total_mb,
    )

    async def require_user_token(
        creds: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> dict[str, Any]:
        if creds is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = await resolve_token_user(store, creds.credentials)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    async def require_admin(
        creds: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> dict[str, Any]:
        user = await require_user_token(creds)
        if user.get("role") != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin role required",
            )
        return user

    async def require_ui_user(
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Any authenticated user, but only from the bundled Web UI.

        Used for management endpoints that owners (not just admins) may use, e.g.
        template management and node template binding. API/agent callers get 404.
        """
        if request.headers.get("x-agent-mesh-ui") != "1":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Not Found"
            )
        return user

    async def require_any_token(
        creds: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> dict[str, Any]:
        """Accept the global token, a user token, or an agent's own token.

        Returns the resolved identity dict:
        ``{"auth": "global"}``, a ``user`` dict (``{"auth": "user", ...}``), or
        ``{"auth": "agent", "agent_id": ..., "device_id": ...}``.
        """
        if creds is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if config.token and constant_time_compare(creds.credentials, config.token):
            return {"auth": "global"}
        user = await resolve_token_user(store, creds.credentials)
        if user is not None:
            return {"auth": "user", **user}
        agent = await store.store.get_agent_by_token_hash(
            hash_token(creds.credentials)
        )
        if agent is not None:
            return {
                "auth": "agent",
                "agent_id": agent.id,
                "device_id": agent.device_id,
            }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def _store_artifact_ref(
        task_id: str, ref: ArtifactRef
    ) -> None:
        await store.store.save_artifact(
            task_id=task_id,
            artifact_id=ref.artifact_id,
            filename=ref.filename,
            size=ref.size,
            content_type=ref.content_type,
            storage_path=str(
                Path(config.artifact_dir) / task_id / f"{ref.artifact_id}_{ref.filename}"
            ),
        )

    from agent_mesh.orchestrator.api.auth import mount_auth_routes
    from agent_mesh.orchestrator.api.agents import mount_agent_routes
    from agent_mesh.orchestrator.api.artifacts import mount_artifact_routes
    from agent_mesh.orchestrator.api.bootstrap import mount_bootstrap_routes
    from agent_mesh.orchestrator.api.edge import mount_edge_routes
    from agent_mesh.orchestrator.api.files import mount_file_routes
    from agent_mesh.orchestrator.api.skills import mount_skill_routes
    from agent_mesh.orchestrator.api.tasks import mount_task_routes
    from agent_mesh.orchestrator.api.teams import mount_team_routes
    from agent_mesh.orchestrator.api.templates import mount_template_routes

    mount_auth_routes(router, store, require_user_token, require_admin, config)
    mount_task_routes(router, store, require_user_token, artifact_store)
    mount_agent_routes(router, store, require_user_token, config, require_admin, require_ui_user)
    mount_edge_routes(router, store, config, require_any_token)
    mount_artifact_routes(router, artifact_store, store, config, require_user_token, require_any_token, _store_artifact_ref)
    mount_file_routes(router, store, config, require_user_token, require_any_token)
    mount_bootstrap_routes(router, config, store, require_user_token, require_any_token, require_admin)
    mount_skill_routes(router, store, config, require_user_token, require_any_token)
    mount_template_routes(router, store, require_ui_user)
    mount_team_routes(router, store, require_admin)

    return router
