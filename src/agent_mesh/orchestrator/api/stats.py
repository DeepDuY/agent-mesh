"""Multi-dimensional dashboard aggregation.

Read-only statistics for the Web console "概览" tab. Every query is
tenancy-scoped: an admin sees everything, while a regular user only sees their
own tasks and those of the teams they belong to (and the nodes they can reach).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import TaskStatus

logger = logging.getLogger(__name__)

# Time-range presets (seconds back from "now"); "custom" uses explicit bounds.
_PRESETS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
_FAILED_STATUSES = {TaskStatus.FAILED.value, TaskStatus.TIMED_OUT.value}
# "Active" = a node is holding it; queued/terminal are tracked separately.
_ACTIVE_STATUSES = {TaskStatus.ASSIGNED.value, TaskStatus.WORKING.value}


def _parse_dt(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {name}: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def mount_stats_routes(router: APIRouter, store: TaskStore, require_user_token) -> None:
    async def _scope(user: dict[str, Any]) -> tuple[str | None, list[str] | None]:
        """(owner_user_id, owner_team_ids); (None, None) means unrestricted."""
        if store.is_admin(user):
            return None, None
        return user.get("user_id"), sorted(await store.user_teams(user))

    @router.get("/stats/overview")
    async def stats_overview(
        range_: str = Query("7d", alias="range"),
        created_after: str | None = None,
        created_before: str | None = None,
        top: int = Query(10, ge=1, le=100),
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        if range_ == "custom":
            after = _parse_dt(created_after, "created_after")
            before = _parse_dt(created_before, "created_before") or now
            if after is None:
                raise HTTPException(
                    status_code=400, detail="created_after required for custom range"
                )
        elif range_ in _PRESETS:
            after = now - _PRESETS[range_]
            before = now
        else:
            raise HTTPException(status_code=400, detail=f"invalid range: {range_}")

        owner_user_id, owner_team_ids = await _scope(user)
        base = dict(
            created_after=after,
            created_before=before,
            owner_user_id=owner_user_id,
            owner_team_ids=owner_team_ids,
        )

        # --- status / mode / totals -------------------------------------
        status_rows = await store.store.stats_task_counts(group_by="status", **base)
        by_status = {r["key"] or "unknown": r["n"] for r in status_rows}
        total = sum(by_status.values())
        completed = by_status.get(TaskStatus.COMPLETED.value, 0)
        failed = sum(by_status.get(s, 0) for s in _FAILED_STATUSES)
        denied = by_status.get(TaskStatus.DENIED.value, 0)
        queued = by_status.get(TaskStatus.QUEUED.value, 0)
        active = sum(by_status.get(s, 0) for s in _ACTIVE_STATUSES)
        terminal = completed + failed
        success_rate = round(completed / terminal, 4) if terminal else None

        mode_rows = await store.store.stats_task_counts(group_by="mode", **base)
        by_mode = {(r["key"] or "llm"): r["n"] for r in mode_rows}

        avg_duration_ms = await store.store.stats_avg_duration(**base)

        # --- per-node / per-user (with the failure slice) ----------------
        failed_statuses = sorted(_FAILED_STATUSES)
        agent_rows = await store.store.stats_task_counts(group_by="agent_id", **base)
        agent_failed = {
            r["key"]: r["n"]
            for r in await store.store.stats_task_counts(
                group_by="agent_id", status_in=failed_statuses, **base
            )
        }
        by_agent = [
            {"agent_id": r["key"], "total": r["n"], "failed": agent_failed.get(r["key"], 0)}
            for r in agent_rows
        ]
        by_agent.sort(key=lambda x: (x["total"], x["failed"]), reverse=True)
        by_agent = by_agent[:top]

        user_rows = await store.store.stats_task_counts(group_by="dispatched_by", **base)
        user_failed = {
            r["key"]: r["n"]
            for r in await store.store.stats_task_counts(
                group_by="dispatched_by", status_in=failed_statuses, **base
            )
        }
        by_user = [
            {"user": r["key"] or "未知", "total": r["n"], "failed": user_failed.get(r["key"], 0)}
            for r in user_rows
        ]
        by_user.sort(key=lambda x: (x["total"], x["failed"]), reverse=True)
        by_user = by_user[:top]

        # --- per-team (a regular user only sees teams they belong to) ----
        team_rows = await store.store.stats_team_counts(**base)
        if owner_team_ids is not None:
            allowed = set(owner_team_ids)
            team_rows = [r for r in team_rows if r["key"] in allowed]
        team_failed = {
            r["key"]: r["n"]
            for r in await store.store.stats_team_counts(
                status_in=failed_statuses, **base
            )
        }
        teams_by_id = {t["team_id"]: t["name"] for t in await store.store.list_teams()}
        by_team = [
            {
                "team_id": r["key"],
                "name": teams_by_id.get(r["key"], r["key"]),
                "total": r["n"],
                "failed": team_failed.get(r["key"], 0),
            }
            for r in team_rows
        ]
        by_team.sort(key=lambda x: x["total"], reverse=True)
        by_team = by_team[:top]

        # --- hourly heatmap (day x hour, rendered in the viewer's tz) ----
        heatmap_rows = await store.store.stats_trend(bucket="hour", **base)
        heat_map: dict[str, dict[str, Any]] = {}
        for r in heatmap_rows:
            b = heat_map.setdefault(r["bucket"], {"bucket": r["bucket"], "total": 0, "failed": 0})
            b["total"] += r["n"]
            if r["status"] in _FAILED_STATUSES:
                b["failed"] += r["n"]
        heatmap = [heat_map[k] for k in sorted(heat_map)]

        # --- node fleet overview ----------------------------------------
        agents = await store.list_agents()
        allowed_ids = await store.accessible_agent_ids(user)
        if allowed_ids is not None:
            agents = [a for a in agents if a.id in allowed_ids]
        agent_summary = {
            "total": len(agents),
            "online": sum(1 for a in agents if a.online),
            "offline": sum(1 for a in agents if not a.online),
        }

        return {
            "range": {
                "preset": range_,
                "created_after": after.isoformat(),
                "created_before": before.isoformat(),
            },
            "summary": {
                "total": total,
                "completed": completed,
                "failed": failed,
                "denied": denied,
                "queued": queued,
                "active": active,
                "success_rate": success_rate,
                "avg_duration_ms": avg_duration_ms,
            },
            "by_status": by_status,
            "by_mode": by_mode,
            "by_agent": by_agent,
            "by_user": by_user,
            "by_team": by_team,
            "heatmap": heatmap,
            "agents": agent_summary,
        }
