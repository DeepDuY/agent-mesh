from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from agent_mesh.orchestrator.artifact_store import ArtifactStore
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore


def mount_artifact_routes(
    router: APIRouter,
    artifact_store: ArtifactStore,
    store: TaskStore,
    config: OrchestratorConfig,
    require_user_token,
    require_any_token,
    _store_artifact_ref,
) -> None:

    @router.post("/artifacts/{task_id}")
    async def upload_artifacts(
        task_id: str,
        files: list[UploadFile] = File(...),
        _auth: None = Depends(require_any_token),
    ) -> dict[str, Any]:
        if not files:
            raise HTTPException(status_code=400, detail="no files provided")
        refs: list[dict[str, Any]] = []
        for upload in files:
            content = await upload.read()
            ref = artifact_store.save(task_id, upload.filename or "unnamed", content)
            await _store_artifact_ref(task_id, ref)
            refs.append(ref.model_dump())
        return {"artifacts": refs}

    @router.get("/artifacts/{task_id}")
    async def list_artifacts(
        task_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        refs = artifact_store.list(task_id)
        return {"artifacts": [r.model_dump() for r in refs]}

    @router.get("/artifacts/{task_id}/{artifact_id}")
    async def download_artifact(
        task_id: str,
        artifact_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ):
        resolved = artifact_store.resolve(task_id, artifact_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        path, filename = resolved
        content_type, _ = mimetypes.guess_type(filename)
        return FileResponse(
            path,
            filename=filename,
            media_type=content_type or "application/octet-stream",
        )
