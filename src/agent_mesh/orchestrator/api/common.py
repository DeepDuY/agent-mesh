"""Shared helpers for the REST layer.

These helpers are used by multiple route modules so query-parameter parsing and
tenancy scoping cannot drift between endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from agent_mesh.orchestrator.task_store import TaskStore


def parse_query_dt(value: str | None, name: str) -> datetime | None:
    """Parse an ISO-8601 query parameter into a UTC-aware datetime, or raise 400."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {name}: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def owner_filter(
    store: TaskStore, user: dict[str, Any]
) -> tuple[str | None, list[str] | None]:
    """Return the ``(owner_user_id, owner_team_ids)`` tenancy filter for a user.

    ``(None, None)`` means unrestricted (admin); otherwise the caller is scoped
    to their own tasks and those of their teams.
    """
    if store.is_admin(user):
        return None, None
    return user.get("user_id"), sorted(await store.user_teams(user))
