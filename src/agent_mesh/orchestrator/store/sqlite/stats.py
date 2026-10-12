from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import _dt_to_iso, SQLiteBase
from agent_mesh.orchestrator.store.sqlite.tasks import _tenancy_clause

# Whitelisted grouping dimensions -> tasks columns. Never interpolate a
# client-supplied string into SQL.
_GROUP_COLS = {
    "status": "status",
    "mode": "mode",
    "agent_id": "agent_id",
    "dispatched_by": "dispatched_by",
}

# created_at is stored as an ISO-8601 string, so substring slicing is both
# correct and cheaper than parsing it back with strftime().
_BUCKET_EXPR = {
    "day": "substr(created_at, 1, 10)",
    "hour": "substr(created_at, 1, 13)",
}


def _stats_where(
    created_after: datetime | None,
    created_before: datetime | None,
    owner_user_id: str | None,
    owner_team_ids: list[str] | None,
    status_in: list[str] | None,
) -> tuple[str, list[Any]]:
    where = " WHERE 1=1"
    params: list[Any] = []
    if created_after is not None:
        where += " AND created_at >= ?"
        params.append(_dt_to_iso(created_after))
    if created_before is not None:
        where += " AND created_at <= ?"
        params.append(_dt_to_iso(created_before))
    if status_in:
        where += " AND status IN (" + ",".join("?" * len(status_in)) + ")"
        params.extend(status_in)
    clause, cparams = _tenancy_clause(owner_user_id, owner_team_ids)
    where += clause
    params.extend(cparams)
    return where, params


class StatsMixin(SQLiteBase):
    """Read-only aggregation for the multi-dimensional dashboard."""

    async def stats_task_counts(
        self,
        *,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        owner_user_id: str | None = None,
        owner_team_ids: list[str] | None = None,
        group_by: str | None = None,
        status_in: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        where, params = _stats_where(
            created_after, created_before, owner_user_id, owner_team_ids, status_in
        )
        if group_by is None:
            sql = f"SELECT '' AS k, COUNT(*) AS n FROM tasks{where}"
        else:
            col = _GROUP_COLS[group_by]
            sql = f"SELECT {col} AS k, COUNT(*) AS n FROM tasks{where} GROUP BY {col}"
        rows = await self._execute(sql, tuple(params))
        return [{"key": r["k"] or "", "n": int(r["n"])} for r in rows]

    async def stats_team_counts(
        self,
        *,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        owner_user_id: str | None = None,
        owner_team_ids: list[str] | None = None,
        status_in: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        where, params = _stats_where(
            created_after, created_before, owner_user_id, owner_team_ids, status_in
        )
        sql = (
            "SELECT value AS k, COUNT(*) AS n "
            "FROM tasks, json_each(tasks.team_ids)" + where + " GROUP BY value"
        )
        rows = await self._execute(sql, tuple(params))
        return [{"key": str(r["k"]), "n": int(r["n"])} for r in rows]

    async def stats_trend(
        self,
        *,
        bucket: str,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        owner_user_id: str | None = None,
        owner_team_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        expr = _BUCKET_EXPR[bucket]
        where, params = _stats_where(
            created_after, created_before, owner_user_id, owner_team_ids, None
        )
        sql = (
            f"SELECT {expr} AS b, status AS s, COUNT(*) AS n FROM tasks{where} "
            f"GROUP BY b, s ORDER BY b"
        )
        rows = await self._execute(sql, tuple(params))
        return [{"bucket": r["b"], "status": r["s"], "n": int(r["n"])} for r in rows]

    async def stats_avg_duration(
        self,
        *,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        owner_user_id: str | None = None,
        owner_team_ids: list[str] | None = None,
    ) -> float | None:
        where, params = _stats_where(
            created_after, created_before, owner_user_id, owner_team_ids, None
        )
        sql = (
            "SELECT AVG(duration_ms) AS m FROM task_results WHERE task_id IN "
            f"(SELECT task_id FROM tasks{where})"
        )
        rows = await self._execute(sql, tuple(params))
        value = rows[0]["m"] if rows else None
        return float(value) if value is not None else None
