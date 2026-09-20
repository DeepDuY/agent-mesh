from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from agent_mesh.orchestrator.artifact_store import ArtifactStore
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import Constraints, FileRef

logger = logging.getLogger(__name__)


def _parse_dt(value: str | None, name: str) -> datetime | None:
    """Parse an ISO-8601 query param into a UTC-aware datetime, or 400."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {name}: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_dt(dt: datetime | None) -> datetime | None:
    """Treat a naive datetime (e.g. from a JSON body) as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _resolve_attachment_refs(
    store: TaskStore, file_ids: list[str], user: dict[str, Any] | None = None
) -> list[FileRef]:
    """Resolve file-library ids into authoritative FileRefs; raise on missing.

    A non-admin may only attach files they own (created_by), so attachments
    cannot be used to reference (and thereby read) another user's files.
    """
    rows = await store.store.get_files_by_ids(file_ids)
    found = {r["file_id"]: r for r in rows}
    for fid in file_ids:
        if fid not in found:
            raise HTTPException(status_code=400, detail=f"attachment file not found: {fid}")
    refs: list[FileRef] = []
    for fid in file_ids:
        r = found[fid]
        if user is not None and not store.is_admin(user):
            owner_ids = {user.get("user_id"), user.get("username")}
            if r.get("created_by") not in owner_ids:
                # Do not reveal the existence of another user's file.
                raise HTTPException(status_code=400, detail=f"attachment file not found: {fid}")
        refs.append(
            FileRef(
                file_id=r["file_id"],
                filename=r["filename"],
                size=r["size"],
                content_type=r.get("content_type") or "application/octet-stream",
                md5=r["md5"],
                download_url=f"/api/files/{r['file_id']}",
            )
        )
    return refs


class _BatchDeletePayload(BaseModel):
    task_ids: list[str] = Field(default_factory=list)
    all_matching: bool = False
    status: str | None = None
    mode: str | None = None
    search: str | None = None
    started_after: datetime | None = None
    started_before: datetime | None = None
    agent_id: str | None = None


def mount_task_routes(
    router: APIRouter,
    store: TaskStore,
    require_user_token,
    artifact_store: ArtifactStore | None = None,
) -> None:

    async def _owner_filter(user: dict[str, Any]):
        """(owner_user_id, owner_team_id) for a non-admin, else (None, None)."""
        if store.is_admin(user):
            return None, None
        return user.get("user_id"), await store.user_team(user)

    async def _require_task(task_id: str, user: dict[str, Any]):
        task = await store.get_task(task_id)
        if task is None or not await _can_see_task(task, user):
            raise HTTPException(status_code=404, detail="task not found")
        return task

    async def _can_see_task(task, user: dict[str, Any]) -> bool:
        return await store.can_see_task(user, task)

    @router.get("/tasks")
    async def list_tasks(
        agent_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        search: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        try:
            task_status = TaskStatus(status) if status else None
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"invalid status: {status}"
            )
        if mode is not None and mode not in ("command", "llm"):
            raise HTTPException(status_code=400, detail="mode must be 'command' or 'llm'")
        after = _parse_dt(started_after, "started_after")
        before = _parse_dt(started_before, "started_before")
        owner_user_id, owner_team_id = await _owner_filter(user)
        tasks = await store.list_tasks(
            agent_id=agent_id,
            status=task_status,
            mode=mode,
            search=search or None,
            started_after=after,
            started_before=before,
            limit=limit,
            offset=offset,
            owner_user_id=owner_user_id,
            owner_team_id=owner_team_id,
        )
        total = await store.store.count_tasks(
            agent_id=agent_id,
            status=status,
            mode=mode,
            search=search or None,
            started_after=after,
            started_before=before,
            owner_user_id=owner_user_id,
            owner_team_id=owner_team_id,
        )
        return {
            "tasks": [t.model_dump_json_safe() for t in tasks],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @router.get("/tasks/{task_id}")
    async def get_task(
        task_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        task = await _require_task(task_id, user)
        return {"task": task.model_dump_json_safe()}

    @router.post("/tasks/dispatch")
    async def rest_dispatch_task(
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        body = await request.json()
        mode = body.get("mode")
        if mode not in ("command", "llm"):
            raise HTTPException(status_code=400, detail="mode must be 'command' or 'llm'")

        # Tenancy: the caller must be allowed to operate the target node.
        target = await store.resolve_agent(body.get("agent_id", ""))
        if target is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if not await store.can_access_agent(user, target):
            raise HTTPException(status_code=403, detail="no access to this node")

        model_error = await store.validate_llm_model(
            body.get("agent_id", ""), mode, body.get("model")
        )
        if model_error:
            raise HTTPException(status_code=400, detail=model_error)

        # Server-side permission pre-check for command mode (immediate feedback;
        # the edge re-evaluates as the enforcement point).
        if mode == "command":
            denial = await store.check_command_permission(
                body.get("agent_id", ""), body.get("instruction", "")
            )
            if denial:
                # Record the rejected attempt as a terminal `denied` task so the
                # user can see it in the task list, then still answer 403.
                denied_id = await store.dispatch_denied(
                    agent_id=body.get("agent_id", ""),
                    instruction=body.get("instruction", ""),
                    mode=mode,
                    reason=denial,
                    dispatched_by=user["username"],
                    user_id=user.get("user_id"),
                    team_id=await store.user_team(user),
                )
                await store.store.append_task_event(
                    task_id=denied_id,
                    event_type="permission_denied",
                    agent_id=body.get("agent_id"),
                    user_id=user.get("user_id"),
                    details={"reason": denial},
                )
                logger.warning(
                    "dispatch denied by permission policy task=%s agent=%s user=%s reason=%s instruction=%r",
                    denied_id,
                    body.get("agent_id"),
                    user.get("user_id"),
                    denial,
                    body.get("instruction", ""),
                )
                raise HTTPException(status_code=403, detail=denial)

        constraints = Constraints(
            workdir=body.get("workdir", "."),
            timeout_s=body.get("timeout_s", 300),
            model=body.get("model"),
            output_limit=body.get("output_limit", 200_000),
            session_id=body.get("session_id"),
            skills=body.get("skills"),
        )
        attachments = await _resolve_attachment_refs(
            store, body.get("attachments") or [], user
        )
        team_id = await store.user_team(user)
        task_id = await store.dispatch(
            agent_id=body.get("agent_id", ""),
            instruction=body.get("instruction", ""),
            mode=mode,
            constraints=constraints,
            max_retries=body.get("max_retries", 0),
            depends_on=body.get("depends_on"),
            dispatched_by=user["username"],
            attachments=attachments,
            user_id=user.get("user_id"),
            team_id=team_id,
        )
        logger.info("REST dispatched task %s by %s", task_id, user["username"])
        await store.store.append_task_event(
            task_id=task_id,
            event_type="dispatched",
            agent_id=body.get("agent_id"),
            user_id=user.get("user_id"),
            details={"mode": mode},
        )
        return {"task_id": task_id, "status": TaskStatus.QUEUED.value}

    @router.get("/tasks/{task_id}/events")
    async def rest_task_events(
        task_id: str,
        limit: int = Query(200, ge=1, le=1000),
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        await _require_task(task_id, user)
        events = await store.store.list_task_events(task_id, limit=limit)
        return {"task_id": task_id, "events": events}

    @router.get("/tasks/{task_id}/logs")
    async def rest_task_logs(
        task_id: str,
        after_id: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=2000),
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        await _require_task(task_id, user)
        logs = await store.store.list_task_logs(task_id, after_id=after_id, limit=limit)
        next_id = logs[-1]["id"] if logs else after_id
        return {"task_id": task_id, "logs": logs, "next_id": next_id}

    @router.get("/tasks/{task_id}/status")
    async def rest_get_task_status(
        task_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        task = await store.get_task(task_id)
        if task is None or not await _can_see_task(task, user):
            return {"found": False}
        return {"found": True, "task": task.model_dump_json_safe()}

    @router.post("/tasks/{task_id}/cancel")
    async def rest_cancel_task(
        task_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        await _require_task(task_id, user)
        accepted = await store.cancel_task(task_id)
        if not accepted:
            return {"accepted": False, "error": "task is already in a terminal state"}
        logger.info("REST cancelled task %s by %s", task_id, user["username"])
        await store.store.append_task_event(
            task_id=task_id,
            event_type="cancelled",
            user_id=user.get("user_id"),
            details={},
        )
        return {"accepted": True, "task_id": task_id, "status": TaskStatus.CANCELLED.value}

    @router.delete("/tasks/{task_id}")
    async def rest_delete_task(
        task_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        await _require_task(task_id, user)
        deleted = await store.store.delete_task(task_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="task not found")
        if artifact_store is not None:
            artifact_store.delete_task(task_id)
        logger.info("REST deleted task %s by %s", task_id, user["username"])
        return {"deleted": True, "task_id": task_id}

    @router.post("/tasks/batch-delete")
    async def rest_batch_delete_tasks(
        payload: _BatchDeletePayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        owner_user_id, owner_team_id = await _owner_filter(user)
        if payload.all_matching:
            task_status = None
            if payload.status is not None:
                try:
                    task_status = TaskStatus(payload.status)
                except ValueError:
                    raise HTTPException(
                        status_code=400, detail=f"invalid status: {payload.status}"
                    )
            if payload.mode is not None and payload.mode not in ("command", "llm"):
                raise HTTPException(
                    status_code=400, detail="mode must be 'command' or 'llm'"
                )
            tasks = await store.list_tasks(
                agent_id=payload.agent_id or None,
                status=task_status,
                mode=payload.mode,
                search=payload.search or None,
                started_after=_normalize_dt(payload.started_after),
                started_before=_normalize_dt(payload.started_before),
                limit=10000,
                offset=0,
                owner_user_id=owner_user_id,
                owner_team_id=owner_team_id,
            )
            task_ids = [t.task_id for t in tasks]
        else:
            # Explicit ids: keep only those the caller may see.
            task_ids = []
            for tid in payload.task_ids:
                t = await store.get_task(tid)
                if t is not None and await _can_see_task(t, user):
                    task_ids.append(tid)

        if not task_ids:
            return {"deleted": 0}
        deleted = await store.store.delete_tasks(task_ids)
        if artifact_store is not None:
            artifact_store.delete_tasks(task_ids)
        logger.info(
            "REST batch-deleted %d tasks (requested %d) by %s",
            deleted, len(task_ids), user["username"],
        )
        return {"deleted": deleted}
