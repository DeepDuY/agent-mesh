"""In-process scheduler for cron-triggered tasks.

Mixed into :class:`~agent_mesh.orchestrator.task_store.TaskStore`. The tick loop
is started by ``SweeperMixin.start_sweepers`` and stops with ``_stop_event``.

Single-process only: the loop runs in the orchestrator process (multi-worker is
not supported, see docs/known-issues.md). An atomic ``claim_schedule`` advance
keeps this safe even if that ever changes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, tzinfo
from typing import Any

from agent_mesh.orchestrator.cron import next_run_after, resolve_timezone
from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import Constraints, FileRef

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = {TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.WORKING}


class SchedulerMixin:
    """Cron schedule ticking. Requires ``self.store``, ``self._stop_event``."""

    _schedule_tick_s = 20.0

    async def resolve_schedule_tz(self, schedule: dict[str, Any] | None = None) -> tzinfo:
        """Effective timezone: schedule override > ``settings.schedule_timezone`` > system."""
        name = ((schedule or {}).get("timezone") or "").strip()
        if not name:
            name = (await self.store.get_setting("schedule_timezone") or "").strip()
        tz = resolve_timezone(name or None)
        if tz is None:
            logger.warning(
                "invalid schedule timezone %r; falling back to system timezone", name
            )
            tz = resolve_timezone(None)
        return tz or timezone.utc

    async def compute_schedule_next_run(
        self,
        cron: str,
        schedule: dict[str, Any] | None = None,
        after: datetime | None = None,
    ) -> datetime:
        tz = await self.resolve_schedule_tz(schedule)
        return next_run_after(cron, after or datetime.now(timezone.utc), tz)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def _sweep_schedules(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._schedule_tick_s
                )
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                break
            try:
                await self.tick_schedules()
            except Exception:  # never let one bad schedule kill the loop
                logger.exception("schedule tick failed")

    async def tick_schedules(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        due = await self.store.list_due_schedules(now, limit=100)
        for schedule in due:
            try:
                await self._run_due_schedule(schedule, now)
            except Exception:
                logger.exception("schedule %s run failed", schedule.get("id"))

    async def _run_due_schedule(self, schedule: dict[str, Any], now: datetime) -> None:
        try:
            next_run = await self.compute_schedule_next_run(
                schedule["cron"], schedule, after=schedule.get("next_run_at") or now
            )
            # No backfill: if we are late (downtime or a slow tick), jump to the
            # next future slot instead of firing a burst of catch-up runs.
            if next_run <= now:
                next_run = await self.compute_schedule_next_run(
                    schedule["cron"], schedule, after=now
                )
        except ValueError as e:
            # A hand-edited/invalid cron would otherwise spin the tick loop.
            await self.store.update_schedule(
                schedule["id"], enabled=False, last_status=f"invalid_cron: {e}"
            )
            logger.warning("disabling schedule %s: %s", schedule.get("name"), e)
            return

        claimed = await self.store.claim_schedule(
            schedule["id"], schedule.get("next_run_at"), next_run
        )
        if not claimed:
            return  # another ticker already handled it

        if await self._schedule_still_running(schedule):
            logger.info("schedule %s skipped: previous task still active", schedule.get("name"))
            await self.store.record_schedule_run(
                schedule["id"],
                last_run_at=now,
                last_task_id=schedule.get("last_task_id"),
                last_status="skipped",
            )
            return

        try:
            task_id = await self.run_schedule(schedule, trigger="scheduled")
            status = "queued"
        except ValueError as e:
            logger.warning("schedule %s dispatch failed: %s", schedule.get("name"), e)
            task_id = None
            status = f"error: {e}"
        await self.store.record_schedule_run(
            schedule["id"], last_run_at=now, last_task_id=task_id, last_status=status
        )

    async def _schedule_still_running(self, schedule: dict[str, Any]) -> bool:
        last_id = schedule.get("last_task_id")
        if not last_id:
            return False
        task = await self.get_task(last_id)
        return task is not None and task.status in _ACTIVE_STATUSES

    # ------------------------------------------------------------------
    # Dispatch (also used by POST /api/schedules/{id}/run)
    # ------------------------------------------------------------------
    async def run_schedule(self, schedule: dict[str, Any], *, trigger: str = "manual") -> str:
        """Dispatch the schedule's task now. Returns the task id (raises on bad node/config)."""
        mode = schedule["mode"]
        agent_ref = schedule["agent_ref"]
        stored = schedule.get("constraints") or {}
        constraints = Constraints(
            workdir=stored.get("workdir", "."),
            timeout_s=stored.get("timeout_s", 300),
            model=stored.get("model"),
            output_limit=stored.get("output_limit", 200_000),
            session_id=stored.get("session_id"),
            skills=stored.get("skills"),
        )
        attachments = await self._resolve_schedule_attachments(schedule.get("attachments") or [])
        user_id = schedule.get("user_id")
        team_id = schedule.get("team_id")
        dispatched_by = schedule.get("created_by") or f"schedule:{schedule['name']}"

        skills_error = await self.validate_skills(constraints.skills)
        if skills_error:
            raise ValueError(skills_error)

        if mode == "command":
            denial = await self.check_command_permission(agent_ref, schedule["instruction"])
            if denial:
                task_id = await self.dispatch_denied(
                    agent_id=agent_ref,
                    instruction=schedule["instruction"],
                    mode=mode,
                    reason=denial,
                    dispatched_by=dispatched_by,
                    user_id=user_id,
                    team_id=team_id,
                )
                await self.store.append_task_event(
                    task_id=task_id,
                    event_type="permission_denied",
                    details={"reason": denial, "schedule_id": schedule["id"], "trigger": trigger},
                )
                return task_id
        else:
            model_error = await self.validate_llm_model(agent_ref, mode, constraints.model)
            if model_error:
                raise ValueError(model_error)

        task_id = await self.dispatch(
            agent_id=agent_ref,
            instruction=schedule["instruction"],
            mode=mode,
            constraints=constraints,
            dispatched_by=dispatched_by,
            attachments=attachments,
            user_id=user_id,
            team_id=team_id,
        )
        await self.store.append_task_event(
            task_id=task_id,
            event_type="scheduled",
            user_id=user_id,
            details={
                "schedule_id": schedule["id"],
                "schedule_name": schedule["name"],
                "trigger": trigger,
            },
        )
        logger.info(
            "schedule %s (%s) dispatched task %s", schedule.get("name"), trigger, task_id
        )
        return task_id

    async def _resolve_schedule_attachments(self, file_ids: list[str]) -> list[FileRef]:
        if not file_ids:
            return []
        rows = await self.store.get_files_by_ids(file_ids)
        found = {r["file_id"]: r for r in rows}
        for fid in file_ids:
            if fid not in found:
                raise ValueError(f"attachment file not found: {fid}")
        return [
            FileRef(
                file_id=found[fid]["file_id"],
                filename=found[fid]["filename"],
                size=found[fid]["size"],
                content_type=found[fid].get("content_type") or "application/octet-stream",
                md5=found[fid]["md5"],
                download_url=f"/api/files/{found[fid]['file_id']}",
            )
            for fid in file_ids
        ]
