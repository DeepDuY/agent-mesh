from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_mesh.orchestrator.cron import validate_cron
from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")


class _ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    cron: str
    timezone: str | None = None
    enabled: bool = True
    agent_id: str
    mode: Literal["command", "llm"] = "llm"
    instruction: str = Field(min_length=1)
    workdir: str = "."
    timeout_s: int = 300
    model: str | None = None
    output_limit: int = 200_000
    session_id: str | None = None
    skills: list[str] | None = None
    attachments: list[str] = Field(default_factory=list)
    owner_user_id: str | None = None  # admin-only assignment
    owner_team_id: str | None = None  # admin-only assignment


class _SchedulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    agent_id: str | None = None
    mode: Literal["command", "llm"] | None = None
    instruction: str | None = Field(default=None, min_length=1)
    workdir: str | None = None
    timeout_s: int | None = None
    model: str | None = None
    output_limit: int | None = None
    session_id: str | None = None
    skills: list[str] | None = None
    attachments: list[str] | None = None
    owner_user_id: str | None = None  # admin-only assignment
    owner_team_id: str | None = None  # admin-only assignment


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _public(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": schedule["id"],
        "name": schedule["name"],
        "cron": schedule["cron"],
        "timezone": schedule.get("timezone"),
        "enabled": bool(schedule.get("enabled")),
        "agent_id": schedule.get("agent_ref"),
        "mode": schedule.get("mode"),
        "instruction": schedule.get("instruction"),
        "constraints": schedule.get("constraints") or {},
        "attachments": schedule.get("attachments") or [],
        "owner_user_id": schedule.get("user_id"),
        "owner_team_id": schedule.get("team_id"),
        "created_by": schedule.get("created_by"),
        "next_run_at": _iso(schedule.get("next_run_at")),
        "last_run_at": _iso(schedule.get("last_run_at")),
        "last_task_id": schedule.get("last_task_id"),
        "last_status": schedule.get("last_status"),
        "created_at": _iso(schedule.get("created_at")),
        "updated_at": _iso(schedule.get("updated_at")),
    }


def _constraints_payload(payload: BaseModel) -> dict[str, Any]:
    return {
        "workdir": getattr(payload, "workdir", "."),
        "timeout_s": getattr(payload, "timeout_s", 300),
        "model": getattr(payload, "model", None),
        "output_limit": getattr(payload, "output_limit", 200_000),
        "session_id": getattr(payload, "session_id", None),
        "skills": getattr(payload, "skills", None),
    }


def mount_schedule_routes(
    router: APIRouter,
    store: TaskStore,
    require_user_token,
) -> None:
    """Cron-scheduled task management.

    Visibility mirrors tasks: admins see everything; a user sees schedules owned
    by their user id or their team. The main agent and the Web UI both call these
    endpoints (a user token is required).
    """

    def _owner_ok(schedule: dict[str, Any], user: dict[str, Any], team: str | None) -> bool:
        if store.is_admin(user):
            return True
        if schedule.get("user_id") and schedule["user_id"] == user.get("user_id"):
            return True
        return bool(team and schedule.get("team_id") == team)

    async def _require_owned(schedule_id: int, user: dict[str, Any]) -> dict[str, Any]:
        schedule = await store.store.get_schedule(schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        if not _owner_ok(schedule, user, await store.user_team(user)):
            raise HTTPException(status_code=404, detail="schedule not found")
        return schedule

    async def _validate_target(payload, existing: dict[str, Any] | None, user: dict[str, Any]) -> None:
        agent_ref = getattr(payload, "agent_id", None) or (existing or {}).get("agent_ref")
        if not agent_ref:
            raise HTTPException(status_code=400, detail="agent_id is required")
        mode = getattr(payload, "mode", None) or (existing or {}).get("mode", "llm")
        target = await store.resolve_agent(agent_ref)
        if target is None:
            raise HTTPException(status_code=404, detail="agent not found")
        if not await store.can_access_agent(user, target):
            raise HTTPException(status_code=403, detail="no access to this node")
        model = getattr(payload, "model", None)
        if model is None and existing is not None:
            model = (existing.get("constraints") or {}).get("model")
        model_error = await store.validate_llm_model(agent_ref, mode, model)
        if model_error:
            raise HTTPException(status_code=400, detail=model_error)

    def _validate_cron_or_400(cron: str) -> None:
        try:
            validate_cron(cron)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid cron: {e}")

    @router.get("/schedules")
    async def list_schedules(
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        team = await store.user_team(user)
        schedules = await store.store.list_schedules()
        if not store.is_admin(user):
            schedules = [s for s in schedules if _owner_ok(s, user, team)]
        return {"schedules": [_public(s) for s in schedules]}

    @router.post("/schedules")
    async def create_schedule(
        payload: _ScheduleCreate,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        name = payload.name.strip()
        if not _NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail="name may only contain letters, digits, '.', '_' and '-'",
            )
        if await store.store.get_schedule_by_name(name) is not None:
            raise HTTPException(status_code=409, detail="schedule name already exists")
        _validate_cron_or_400(payload.cron)
        await _validate_target(payload, None, user)
        skills_error = await store.validate_skills(payload.skills)
        if skills_error:
            raise HTTPException(status_code=400, detail=skills_error)
        if store.is_admin(user):
            owner_user_id, owner_team_id = payload.owner_user_id, payload.owner_team_id
        else:
            team = await store.user_team(user)
            owner_user_id = None if team else user.get("user_id")
            owner_team_id = team
        next_run = await store.compute_schedule_next_run(payload.cron, {"timezone": payload.timezone})
        schedule_id = await store.store.create_schedule(
            name=name,
            cron=payload.cron,
            timezone=payload.timezone,
            enabled=payload.enabled,
            agent_ref=payload.agent_id,
            mode=payload.mode,
            instruction=payload.instruction,
            constraints=_constraints_payload(payload),
            attachments=payload.attachments,
            user_id=owner_user_id,
            team_id=owner_team_id,
            created_by=user["username"],
            next_run_at=next_run,
        )
        logger.info("created schedule %s (%s) by %s", schedule_id, name, user["username"])
        return {"schedule": _public(await store.store.get_schedule(schedule_id))}

    @router.get("/schedules/{schedule_id}")
    async def get_schedule(
        schedule_id: int,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        schedule = await _require_owned(schedule_id, user)
        return {"schedule": _public(schedule)}

    @router.patch("/schedules/{schedule_id}")
    async def patch_schedule(
        schedule_id: int,
        payload: _SchedulePatch,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        schedule = await _require_owned(schedule_id, user)
        fields = payload.model_dump(exclude_unset=True)
        if not store.is_admin(user):
            fields.pop("owner_user_id", None)
            fields.pop("owner_team_id", None)

        if "name" in fields and fields["name"] is not None:
            name = fields["name"].strip()
            if not _NAME_RE.match(name):
                raise HTTPException(
                    status_code=400,
                    detail="name may only contain letters, digits, '.', '_' and '-'",
                )
            existing = await store.store.get_schedule_by_name(name)
            if existing is not None and existing["id"] != schedule_id:
                raise HTTPException(status_code=409, detail="schedule name already exists")
            fields["name"] = name
        if "cron" in fields and fields["cron"] is not None:
            _validate_cron_or_400(fields["cron"])
        if any(k in fields for k in ("agent_id", "mode", "model")):
            await _validate_target(payload, schedule, user)
        if fields.get("skills"):
            skills_error = await store.validate_skills(fields["skills"])
            if skills_error:
                raise HTTPException(status_code=400, detail=skills_error)

        # Map request fields onto stored columns / constraints.
        update: dict[str, Any] = {}
        for key in ("name", "cron", "timezone", "enabled", "mode", "instruction"):
            if key in fields:
                update[key] = fields[key]
        if "agent_id" in fields and fields["agent_id"] is not None:
            update["agent_ref"] = fields["agent_id"]
        if "attachments" in fields:
            update["attachments"] = fields["attachments"]
        if store.is_admin(user):
            if "owner_user_id" in fields:
                update["user_id"] = fields["owner_user_id"]
            if "owner_team_id" in fields:
                update["team_id"] = fields["owner_team_id"]
        if any(k in fields for k in ("workdir", "timeout_s", "model", "output_limit", "session_id", "skills")):
            constraints = dict(schedule.get("constraints") or {})
            for key in ("workdir", "timeout_s", "model", "output_limit", "session_id", "skills"):
                if key in fields:
                    constraints[key] = fields[key]
            update["constraints"] = constraints

        # Recompute the next run when the cadence/timezone changes or it is
        # (re-)enabled without a pending run.
        recompute = any(k in fields for k in ("cron", "timezone", "enabled"))
        if recompute:
            cron = update.get("cron", schedule["cron"])
            tz = update.get("timezone", schedule.get("timezone"))
            if update.get("enabled", schedule.get("enabled")):
                update["next_run_at"] = await store.compute_schedule_next_run(
                    cron, {"timezone": tz}
                )
            else:
                update["next_run_at"] = schedule.get("next_run_at")

        await store.store.update_schedule(schedule_id, **update)
        logger.info("updated schedule %s by %s", schedule_id, user["username"])
        return {"schedule": _public(await store.store.get_schedule(schedule_id))}

    @router.delete("/schedules/{schedule_id}")
    async def delete_schedule(
        schedule_id: int,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        await _require_owned(schedule_id, user)
        deleted = await store.store.delete_schedule(schedule_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="schedule not found")
        logger.info("deleted schedule %s by %s", schedule_id, user["username"])
        return {"deleted": True}

    @router.post("/schedules/{schedule_id}/run")
    async def run_schedule_now(
        schedule_id: int,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        schedule = await _require_owned(schedule_id, user)
        try:
            task_id = await store.run_schedule(schedule, trigger="manual")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await store.store.record_schedule_run(
            schedule_id,
            last_run_at=datetime.now(timezone.utc),
            last_task_id=task_id,
            last_status="queued",
        )
        return {"task_id": task_id}
