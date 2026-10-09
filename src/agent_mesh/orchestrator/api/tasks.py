from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from agent_mesh.orchestrator.artifact_store import ArtifactStore
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.limits import clamp_int
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import Constraints, FileRef

logger = logging.getLogger(__name__)

# Fan-out guardrails for batch dispatch. Read from settings on each request so
# an operator can tune them without a restart.
DEFAULT_MAX_BATCH_FANOUT = 200
DEFAULT_MAX_RUN_PER_NODE = 50
_ACTIVE_RUN_STATUSES = {
    TaskStatus.QUEUED.value,
    TaskStatus.ASSIGNED.value,
    TaskStatus.WORKING.value,
}


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


class _DispatchBatchPayload(BaseModel):
    targets: list[Any] = Field(default_factory=list)
    mode: str
    instruction: str
    workdir: str = "."
    timeout_s: int = 300
    model: str | None = None
    output_limit: int = 200_000
    session_id: str | None = None
    skills: list[str] | None = None
    attachments: list[str] = Field(default_factory=list)
    attachments_from: list[str] = Field(default_factory=list)
    max_retries: int = 0
    depends_on: list[str] | None = None
    depends_on_run: str | None = None
    run_id: str | None = None
    idempotency_key: str | None = None


async def _materialize_artifacts(
    store: TaskStore,
    config: OrchestratorConfig,
    artifact_store: ArtifactStore | None,
    task_ids: list[str],
    user: dict[str, Any],
) -> list[FileRef]:
    """Turn upstream task artifacts into library files and attach them.

    ``attachments_from`` lets a downstream task consume an upstream task's
    artifacts without the caller downloading and re-uploading them. Each
    artifact is copied into the file library (deduped by md5) so the edge can
    fetch it with its own token via the normal attachment path.
    """
    if artifact_store is None:
        raise HTTPException(status_code=400, detail="artifact store unavailable")
    files_dir = Path(config.db_path).parent / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    owner = user.get("user_id") or user.get("username")
    refs: list[FileRef] = []
    for tid in task_ids:
        task = await store.get_task(tid)
        if task is None or not await store.can_see_task(user, task):
            raise HTTPException(status_code=400, detail=f"attachments_from task not found: {tid}")
        for art in artifact_store.list(tid):
            resolved = artifact_store.resolve(tid, art.artifact_id)
            if resolved is None:
                continue
            path, filename = resolved
            content = Path(path).read_bytes()
            safe = Path(filename).name
            md5 = hashlib.md5(content).hexdigest()
            existing = await store.store.find_file_by_md5(md5, safe)
            if existing:
                refs.append(
                    FileRef(
                        file_id=existing["file_id"],
                        filename=existing["filename"],
                        size=existing["size"],
                        content_type=existing.get("content_type") or "application/octet-stream",
                        md5=existing["md5"],
                        download_url=f"/api/files/{existing['file_id']}",
                    )
                )
                continue
            file_id = f"f-{uuid.uuid4().hex[:8]}"
            dest = files_dir / f"{file_id}_{safe}"
            dest.write_bytes(content)
            content_type, _ = mimetypes.guess_type(safe)
            content_type = content_type or "application/octet-stream"
            await store.store.create_file(
                file_id=file_id,
                filename=safe,
                size=len(content),
                content_type=content_type,
                md5=md5,
                created_by=owner,
            )
            refs.append(
                FileRef(
                    file_id=file_id,
                    filename=safe,
                    size=len(content),
                    content_type=content_type,
                    md5=md5,
                    download_url=f"/api/files/{file_id}",
                )
            )
    return refs


def _compact_task(task: Any) -> dict[str, Any]:
    """One-line-per-task summary for run monitoring (no logs, bounded output)."""
    result = task.result
    return {
        "task_id": task.task_id,
        "agent_id": task.agent_id,
        "mode": task.mode,
        "status": task.status.value,
        "run_id": task.run_id,
        "exit_code": result.exit_code if result else None,
        "summary": (result.summary if result else "") or "",
        "duration_ms": result.duration_ms if result else None,
        "artifact_count": len(result.artifacts) if result else 0,
        "session_id": result.session_id if result else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
    }


def mount_task_routes(
    router: APIRouter,
    store: TaskStore,
    require_user_token,
    artifact_store: ArtifactStore | None = None,
    config: OrchestratorConfig | None = None,
) -> None:

    async def _owner_filter(user: dict[str, Any]):
        """(owner_user_id, owner_team_idss) for a non-admin, else (None, None)."""
        if store.is_admin(user):
            return None, None
        return user.get("user_id"), sorted(await store.user_teams(user))

    async def _require_task(task_id: str, user: dict[str, Any]):
        task = await store.get_task(task_id)
        if task is None or not await _can_see_task(task, user):
            raise HTTPException(status_code=404, detail="task not found")
        return task

    async def _can_see_task(task, user: dict[str, Any]) -> bool:
        return await store.can_see_task(user, task)

    async def _expand_depends(depends_on, depends_on_run, user) -> list[str]:
        """Combine explicit depends_on with every task in a referenced run."""
        deps = list(depends_on or [])
        if depends_on_run:
            owner_user_id, owner_team_ids = await _owner_filter(user)
            ids = await store.list_run_task_ids(
                depends_on_run,
                owner_user_id=owner_user_id,
                owner_team_ids=owner_team_ids,
            )
            if not ids:
                raise HTTPException(
                    status_code=400, detail=f"depends_on_run not found: {depends_on_run}"
                )
            deps.extend(ids)
        return deps

    async def _resolve_attachments(
        file_ids: list[str], from_task_ids: list[str], user: dict[str, Any]
    ) -> list[FileRef]:
        refs = await _resolve_attachment_refs(store, file_ids or [], user)
        if from_task_ids:
            if config is None:
                raise HTTPException(
                    status_code=400, detail="attachments_from unsupported"
                )
            refs = list(refs) + await _materialize_artifacts(
                store, config, artifact_store, from_task_ids, user
            )
        return refs

    async def _fanout_limits() -> tuple[int, int]:
        fanout = clamp_int(
            await store.store.get_setting("max_batch_fanout"),
            DEFAULT_MAX_BATCH_FANOUT, 1, 100000,
        )
        per_node = clamp_int(
            await store.store.get_setting("max_run_per_node"),
            DEFAULT_MAX_RUN_PER_NODE, 1, 100000,
        )
        return fanout, per_node

    @router.get("/tasks")
    async def list_tasks(
        agent_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        search: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
        run_id: str | None = None,
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
        owner_user_id, owner_team_ids = await _owner_filter(user)
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
            owner_team_ids=owner_team_ids,
            run_id=run_id,
        )
        total = await store.store.count_tasks(
            agent_id=agent_id,
            status=status,
            mode=mode,
            search=search or None,
            started_after=after,
            started_before=before,
            owner_user_id=owner_user_id,
            owner_team_ids=owner_team_ids,
            run_id=run_id,
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

        skills_error = await store.validate_skills(body.get("skills"))
        if skills_error:
            raise HTTPException(status_code=400, detail=skills_error)

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
                    team_ids=sorted(await store.user_teams(user)),
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
        attachments = await _resolve_attachments(
            body.get("attachments") or [],
            body.get("attachments_from") or [],
            user,
        )
        try:
            deps = await _expand_depends(
                body.get("depends_on"), body.get("depends_on_run"), user
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        team_ids = sorted(await store.user_teams(user))
        try:
            task_id = await store.dispatch(
                agent_id=body.get("agent_id", ""),
                instruction=body.get("instruction", ""),
                mode=mode,
                constraints=constraints,
                max_retries=body.get("max_retries", 0),
                depends_on=deps,
                dispatched_by=user["username"],
                attachments=attachments,
                user_id=user.get("user_id"),
                team_ids=team_ids,
                run_id=body.get("run_id"),
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        logger.info("REST dispatched task %s by %s", task_id, user["username"])
        await store.store.append_task_event(
            task_id=task_id,
            event_type="dispatched",
            agent_id=body.get("agent_id"),
            user_id=user.get("user_id"),
            details={"mode": mode, "run_id": body.get("run_id")},
        )
        return {"task_id": task_id, "status": TaskStatus.QUEUED.value}

    @router.post("/tasks/dispatch-batch")
    async def rest_dispatch_batch(
        payload: _DispatchBatchPayload,
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Dispatch the same task to many nodes in one call (a "run").

        Each target is validated independently (node ACL, model allow-list,
        skills, command policy); a target that fails is reported in ``denied``
        rather than aborting the whole batch. All accepted tasks share one
        ``run_id`` so progress can be read with a single ``GET /runs/{run_id}``.
        """
        if payload.mode not in ("command", "llm"):
            raise HTTPException(status_code=400, detail="mode must be 'command' or 'llm'")
        if not payload.targets:
            raise HTTPException(status_code=400, detail="targets must not be empty")

        targets: list[Any] = []
        seen: set[str] = set()
        for t in payload.targets:
            key = str(t)
            if key in seen:
                continue
            seen.add(key)
            targets.append(t)

        max_fanout, max_per_node = await _fanout_limits()
        if len(targets) > max_fanout:
            raise HTTPException(
                status_code=400,
                detail=f"batch fan-out {len(targets)} exceeds limit {max_fanout}",
            )

        # Idempotency: a repeated request (same key) returns the original run.
        idem_key = (
            payload.idempotency_key or request.headers.get("Idempotency-Key") or ""
        ).strip()
        idem_scope = f"batch:{user.get('user_id') or user.get('username')}"
        if idem_key:
            existing = await store.store.get_idempotent_run(idem_scope, idem_key)
            if existing:
                ids = await store.list_run_task_ids(existing)
                return {
                    "run_id": existing,
                    "queued": 0,
                    "task_ids": ids,
                    "denied": [],
                    "duplicate": True,
                }

        run_id = (payload.run_id or "").strip() or store.new_run_id()

        skills_error = await store.validate_skills(payload.skills)
        if skills_error:
            raise HTTPException(status_code=400, detail=skills_error)

        constraints = Constraints(
            workdir=payload.workdir,
            timeout_s=payload.timeout_s,
            model=payload.model,
            output_limit=payload.output_limit,
            session_id=payload.session_id,
            skills=payload.skills,
        )
        try:
            base_attachments = await _resolve_attachments(
                payload.attachments, payload.attachments_from, user
            )
            base_deps = await _expand_depends(
                payload.depends_on, payload.depends_on_run, user
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        team_ids = sorted(await store.user_teams(user))
        task_ids: list[str] = []
        denied: list[dict[str, Any]] = []
        per_node: dict[str, int] = {}

        for raw in targets:
            ref = str(raw)
            target = await store.resolve_agent(ref)
            if target is None:
                denied.append({"target": raw, "reason": "agent not found"})
                continue
            if not await store.can_access_agent(user, target):
                denied.append({"target": raw, "reason": "no access to this node"})
                continue
            queue_key = target.device_id or target.agent_id
            if per_node.get(queue_key, 0) >= max_per_node:
                denied.append(
                    {
                        "target": raw,
                        "reason": f"per-node fan-out limit {max_per_node} exceeded",
                    }
                )
                continue
            model_error = await store.validate_llm_model(ref, payload.mode, payload.model)
            if model_error:
                denied.append({"target": raw, "reason": model_error})
                continue
            if payload.mode == "command":
                denial = await store.check_command_permission(ref, payload.instruction)
                if denial:
                    denied_id = await store.dispatch_denied(
                        agent_id=ref,
                        instruction=payload.instruction,
                        mode=payload.mode,
                        reason=denial,
                        dispatched_by=user["username"],
                        user_id=user.get("user_id"),
                        team_ids=team_ids,
                    )
                    denied.append({"target": raw, "reason": denial, "task_id": denied_id})
                    continue
            try:
                tid = await store.dispatch(
                    agent_id=ref,
                    instruction=payload.instruction,
                    mode=payload.mode,
                    constraints=constraints,
                    max_retries=payload.max_retries,
                    depends_on=base_deps,
                    dispatched_by=user["username"],
                    attachments=base_attachments,
                    user_id=user.get("user_id"),
                    team_ids=team_ids,
                    run_id=run_id,
                )
            except ValueError as e:
                denied.append({"target": raw, "reason": str(e)})
                continue
            per_node[queue_key] = per_node.get(queue_key, 0) + 1
            task_ids.append(tid)
            await store.store.append_task_event(
                task_id=tid,
                event_type="dispatched",
                agent_id=ref,
                user_id=user.get("user_id"),
                details={"mode": payload.mode, "run_id": run_id, "batch": True},
            )

        if idem_key:
            await store.store.set_idempotent_run(idem_scope, idem_key, run_id)

        logger.info(
            "REST batch-dispatched run=%s queued=%d denied=%d by %s",
            run_id, len(task_ids), len(denied), user["username"],
        )
        return {
            "run_id": run_id,
            "queued": len(task_ids),
            "task_ids": task_ids,
            "denied": denied,
        }

    @router.get("/runs/{run_id}")
    async def rest_get_run(
        run_id: str,
        status: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
        offset: int = Query(0, ge=0),
        wait: float = Query(0, ge=0, le=30),
        include_tasks: bool = True,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Aggregate a run: status counts + one compact row per task.

        ``wait`` (up to 30s) long-polls until any task changes state or the run
        leaves the active states, so a caller (e.g. the main agent) can block
        instead of polling.
        """
        owner_user_id, owner_team_ids = await _owner_filter(user)
        task_status = None
        if status is not None:
            try:
                task_status = TaskStatus(status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"invalid status: {status}")

        counts = await store.run_status_counts(run_id, owner_user_id, owner_team_ids)
        if not counts:
            raise HTTPException(status_code=404, detail="run not found")

        if wait and any(s in counts for s in _ACTIVE_RUN_STATUSES):
            deadline = time.monotonic() + min(wait, 30.0)
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                new_counts = await store.run_status_counts(
                    run_id, owner_user_id, owner_team_ids
                )
                if new_counts != counts:
                    counts = new_counts
                    break
                if not any(s in counts for s in _ACTIVE_RUN_STATUSES):
                    break

        resp: dict[str, Any] = {
            "run_id": run_id,
            "total": sum(counts.values()),
            "counts": counts,
            "active": any(s in counts for s in _ACTIVE_RUN_STATUSES),
        }
        if include_tasks:
            tasks = await store.list_tasks(
                run_id=run_id,
                status=task_status,
                limit=limit,
                offset=offset,
                owner_user_id=owner_user_id,
                owner_team_ids=owner_team_ids,
            )
            resp["tasks"] = [_compact_task(t) for t in tasks]
            resp["limit"] = limit
            resp["offset"] = offset
        return resp

    @router.post("/runs/{run_id}/cancel")
    async def rest_cancel_run(
        run_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        owner_user_id, owner_team_ids = await _owner_filter(user)
        counts = await store.run_status_counts(run_id, owner_user_id, owner_team_ids)
        if not counts:
            raise HTTPException(status_code=404, detail="run not found")
        result = await store.cancel_run(run_id, owner_user_id, owner_team_ids)
        logger.info("REST cancelled run %s (%s) by %s", run_id, result, user["username"])
        return {"run_id": run_id, **result}

    @router.post("/runs/{run_id}/retry")
    async def rest_retry_run(
        run_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        owner_user_id, owner_team_ids = await _owner_filter(user)
        counts = await store.run_status_counts(run_id, owner_user_id, owner_team_ids)
        if not counts:
            raise HTTPException(status_code=404, detail="run not found")
        result = await store.retry_run(run_id, owner_user_id, owner_team_ids)
        logger.info("REST retried run %s (%s) by %s", run_id, result, user["username"])
        return {"run_id": run_id, **result}

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
        owner_user_id, owner_team_ids = await _owner_filter(user)
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
                owner_team_ids=owner_team_ids,
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
