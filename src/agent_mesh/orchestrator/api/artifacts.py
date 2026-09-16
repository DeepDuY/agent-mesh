from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from agent_mesh.orchestrator.artifact_store import ArtifactStore, ArtifactTooLarge
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.limits import MB, get_int_setting
from agent_mesh.orchestrator.task_store import TaskStore


async def _ensure_global_capacity(
    artifact_store: ArtifactStore,
    store: TaskStore,
    needed: int,
    global_total: int,
    evict_oldest: bool,
) -> None:
    """Free space for ``needed`` bytes, recycling the oldest artifacts.

    When ``evict_oldest`` is on (default), artifacts are deleted oldest-first
    until the new upload fits under the global quota; their DB rows are removed
    too. When it is off, an over-quota upload is rejected outright.
    """
    used = artifact_store.total_size()
    if used + needed <= global_total:
        return
    if not evict_oldest:
        raise HTTPException(status_code=413, detail="artifact storage quota exceeded")
    for item in artifact_store.oldest_files():
        if used + needed <= global_total:
            break
        artifact_store.remove(item["path"])
        await store.store.delete_artifact(item["artifact_id"])
        used -= item["size"]
    if used + needed > global_total:
        raise HTTPException(
            status_code=413,
            detail="artifact storage quota exceeded (nothing left to evict)",
        )


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
        auth: dict[str, Any] = Depends(require_any_token),
    ) -> dict[str, Any]:
        if not files:
            raise HTTPException(status_code=400, detail="no files provided")
        task = await store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if not await store.edge_can_access_task(auth, task):
            raise HTTPException(status_code=403, detail="no access to this task")

        # Effective limits are read per request from settings so a config-page
        # change applies immediately (and in every worker).
        max_per_file = await get_int_setting(store, "artifact_max_size_mb") * MB
        task_total = await get_int_setting(store, "artifact_task_total_mb") * MB
        global_total = await get_int_setting(store, "artifact_total_mb") * MB
        evict_oldest = bool(await get_int_setting(store, "artifact_evict_oldest"))

        refs: list[dict[str, Any]] = []
        for upload in files:
            content = await upload.read()
            if len(content) > max_per_file:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"artifact {upload.filename} exceeds max size "
                        f"{max_per_file // MB}MB"
                    ),
                )
            await _ensure_global_capacity(
                artifact_store, store, len(content), global_total, evict_oldest
            )
            try:
                ref = artifact_store.save(
                    task_id,
                    upload.filename or "unnamed",
                    content,
                    max_size=max_per_file,
                    task_total=task_total,
                )
            except ArtifactTooLarge as e:
                raise HTTPException(status_code=413, detail=str(e))
            await _store_artifact_ref(task_id, ref)
            refs.append(ref.model_dump())
        return {"artifacts": refs}

    @router.get("/artifacts/{task_id}")
    async def list_artifacts(
        task_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        task = await store.get_task(task_id)
        if task is None or not await store.can_see_task(user, task):
            raise HTTPException(status_code=404, detail="task not found")
        refs = artifact_store.list(task_id)
        return {"artifacts": [r.model_dump() for r in refs]}

    @router.get("/artifacts/{task_id}/{artifact_id}")
    async def download_artifact(
        task_id: str,
        artifact_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ):
        task = await store.get_task(task_id)
        if task is None or not await store.can_see_task(user, task):
            raise HTTPException(status_code=404, detail="task not found")
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
