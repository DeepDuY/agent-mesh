from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from agent_mesh.orchestrator.auth import (
    generate_token,
    hash_password,
    hash_token,
    make_session_token,
    verify_password,
)
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import SERVER_VERSION

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

_ADMIN_USERNAME = "admin"


def _user_public(
    user: dict[str, Any], teams: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Safe projection of a user row for list/me responses (no token_hash).

    A user may belong to several teams; ``team_id``/``team_name`` keep the first
    for backward compatibility while ``team_ids``/``team_names`` expose all.
    """
    teams = teams or []
    return {
        "user_id": user.get("user_id"),
        "username": user.get("username"),
        "role": user.get("role"),
        "disabled": bool(user.get("disabled")),
        "created_by": user.get("created_by"),
        "team_ids": [t["team_id"] for t in teams],
        "team_names": [t["name"] for t in teams],
        "team_id": teams[0]["team_id"] if teams else None,
        "team_name": teams[0]["name"] if teams else None,
        "created_at": _iso(user.get("created_at")),
        "last_login_at": _iso(user.get("last_login_at")),
        "token_created_at": _iso(user.get("token_created_at")),
        "token_expires_at": _iso(user.get("token_expires_at")),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class _LoginPayload(BaseModel):
    username: str
    password: str


class _CreateUserPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = "user"
    team_id: str | None = None  # legacy single-team field
    team_ids: list[str] | None = None


class _SetPasswordPayload(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class _ChangePasswordPayload(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class _RotateTokenPayload(BaseModel):
    # Blank/null means the new token never expires.
    expires_in_days: int | None = Field(default=None, ge=1, le=36500)


def _expiry_from_days(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


def mount_auth_routes(
    router: APIRouter,
    store: TaskStore,
    require_user_token,
    require_admin,
    config: OrchestratorConfig | None = None,
) -> None:
    session_ttl_s = (config.session_ttl_s if config else 86400)

    async def _teams_of(user_id: str) -> list[dict[str, Any]]:
        teams: list[dict[str, Any]] = []
        for team_id in await store.store.get_user_teams(user_id):
            team = await store.store.get_team(team_id)
            if team is not None:
                teams.append({"team_id": team_id, "name": team["name"]})
        return teams

    @router.post("/auth/login")
    async def login(payload: _LoginPayload) -> dict[str, Any]:
        if not payload.username or not payload.password:
            raise HTTPException(status_code=400, detail="username and password required")
        user = await store.store.get_user_by_username(payload.username)
        if user is None or not verify_password(payload.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="invalid credentials")
        if user.get("disabled"):
            raise HTTPException(status_code=403, detail="user is disabled")
        secret = await store.store.get_setting("session_secret")
        if not secret:
            # Without a signing secret any "signed" session token could be
            # forged. Fail closed instead of issuing tokens under an empty secret.
            raise HTTPException(
                status_code=500, detail="session secret not configured"
            )
        session_token = make_session_token(
            payload.username, secret, ttl_s=session_ttl_s
        )
        await store.store.set_user_last_login(user["user_id"], datetime.now(timezone.utc))
        return {
            "username": payload.username,
            "role": user["role"],
            "token": session_token,
            "token_type": "session",
        }

    @router.get("/auth/me")
    async def me(user: dict[str, Any] = Depends(require_user_token)):
        return _user_public(user, await _teams_of(user["user_id"]))

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": SERVER_VERSION}

    # ------------------------------------------------------------------
    # Admin-only user management
    # ------------------------------------------------------------------
    @router.post("/auth/users")
    async def create_user(
        payload: _CreateUserPayload,
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        username = payload.username.strip()
        if not _USERNAME_RE.match(username):
            raise HTTPException(
                status_code=400,
                detail="username may only contain letters, digits, '.', '_' and '-'",
            )
        if username == _ADMIN_USERNAME:
            raise HTTPException(status_code=400, detail="cannot create a second admin")
        if payload.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
        raw_team_ids = payload.team_ids or ([payload.team_id] if payload.team_id else [])
        team_ids = [t for t in (x.strip() for x in raw_team_ids if x) if t]
        # De-duplicate while preserving order.
        team_ids = list(dict.fromkeys(team_ids))
        if not team_ids:
            raise HTTPException(status_code=400, detail="team_ids is required")
        for team_id in team_ids:
            if await store.store.get_team(team_id) is None:
                raise HTTPException(status_code=400, detail=f"team not found: {team_id}")
        existing = await store.store.get_user_by_username(username)
        if existing is not None:
            raise HTTPException(status_code=409, detail="username already exists")
        token = generate_token()
        user_id = f"u-{token[:8]}"
        now = datetime.now(timezone.utc)
        await store.store.create_user(
            user_id=user_id,
            username=username,
            password_hash=hash_password(payload.password),
            token_hash=hash_token(token),
            role=payload.role,
            created_by=admin["username"],
            token_created_at=now,
        )
        # A user must belong to at least one team, but may belong to several.
        for team_id in team_ids:
            await store.store.add_user_to_team(user_id, team_id)
        logger.info(
            "created user %s by %s (role=%s, teams=%s)",
            username, admin["username"], payload.role, team_ids,
        )
        # The API token is returned exactly once, at creation.
        return {
            "user_id": user_id,
            "username": username,
            "role": payload.role,
            "team_id": team_ids[0],
            "team_ids": team_ids,
            "token": token,
            "token_type": "api",
        }

    @router.get("/auth/users")
    async def list_users(
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        users = await store.store.list_users()
        teams = await store.store.list_teams()
        teams_of: dict[str, list[dict[str, Any]]] = {}
        for team in teams:
            for member_id in await store.store.list_team_members(team["team_id"]):
                teams_of.setdefault(member_id, []).append(team)
        return {
            "users": [
                _user_public(u, teams_of.get(u["user_id"], [])) for u in users
            ]
        }

    @router.delete("/auth/users/{username}")
    async def delete_user(
        username: str,
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        user = await store.store.get_user_by_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if user["user_id"] == admin["user_id"]:
            raise HTTPException(status_code=400, detail="cannot delete your own account")
        if username == _ADMIN_USERNAME:
            raise HTTPException(status_code=400, detail="cannot delete the admin account")
        deleted = await store.store.delete_user(user["user_id"])
        logger.info("deleted user %s by %s", username, admin["username"])
        return {"deleted": deleted}

    @router.post("/auth/users/{username}/token")
    async def rotate_user_token(
        username: str,
        payload: _RotateTokenPayload | None = None,
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        user = await store.store.get_user_by_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        token = generate_token()
        expires_at = _expiry_from_days(payload.expires_in_days if payload else None)
        await store.store.set_user_token(
            user["user_id"],
            hash_token(token),
            datetime.now(timezone.utc),
            expires_at,
        )
        logger.info("rotated token for user %s by %s", username, admin["username"])
        # New token is returned exactly once.
        return {
            "user_id": user["user_id"],
            "username": username,
            "token": token,
            "token_type": "api",
            "token_expires_at": _iso(expires_at),
        }

    @router.post("/auth/users/{username}/password")
    async def set_user_password(
        username: str,
        payload: _SetPasswordPayload,
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        user = await store.store.get_user_by_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        await store.store.set_user_password(user["user_id"], hash_password(payload.password))
        logger.info("reset password for user %s by %s", username, admin["username"])
        return {"ok": True}

    # ------------------------------------------------------------------
    # Self-service
    # ------------------------------------------------------------------
    @router.post("/auth/change-password")
    async def change_own_password(
        payload: _ChangePasswordPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        if not verify_password(payload.old_password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="old password is incorrect")
        await store.store.set_user_password(user["user_id"], hash_password(payload.new_password))
        return {"ok": True}

    @router.post("/auth/token")
    async def rotate_own_token(
        payload: _RotateTokenPayload | None = None,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Rotate the caller's own API token (returned once).

        ``expires_in_days`` sets an optional lifetime; omitted/null means the
        token never expires. The previous API token stops working immediately;
        any existing session token is unaffected.
        """
        token = generate_token()
        expires_at = _expiry_from_days(payload.expires_in_days if payload else None)
        await store.store.set_user_token(
            user["user_id"],
            hash_token(token),
            datetime.now(timezone.utc),
            expires_at,
        )
        logger.info("rotated own token for user %s", user["username"])
        return {
            "user_id": user["user_id"],
            "username": user["username"],
            "token": token,
            "token_type": "api",
            "token_expires_at": _iso(expires_at),
        }
