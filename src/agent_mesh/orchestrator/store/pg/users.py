from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_mesh.orchestrator.store.pg.base import PostgresBase


class UsersMixin(PostgresBase):
    """User accounts and their credentials/tokens."""

    async def create_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        token_hash: str,
        role: str = "user",
        disabled: bool = False,
        created_by: str | None = None,
        token_created_at: datetime | None = None,
        token_expires_at: datetime | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO users (user_id, username, password_hash, token_hash, role, disabled, created_by, token_created_at, token_expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, password_hash, token_hash, role, disabled, created_by,
             token_created_at, token_expires_at),
        )

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT user_id, username, password_hash, role, disabled, created_by, created_at, last_login_at, token_created_at, token_expires_at, token_hash "
            "FROM users WHERE username = ?",
            (username,),
        )
        return dict(row) if row else None

    async def get_user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        row = await self._db.fetchrow(
            "SELECT user_id, username, password_hash, role, disabled, created_by, created_at, last_login_at, token_created_at, token_expires_at, token_hash "
            "FROM users WHERE token_hash = ?",
            (token_hash,),
        )
        return dict(row) if row else None

    async def list_users(self) -> list[dict[str, Any]]:
        rows = await self._db.execute(
            "SELECT user_id, username, role, disabled, created_by, created_at, last_login_at, token_created_at, token_expires_at "
            "FROM users ORDER BY created_at ASC"
        )
        return [dict(row) for row in rows]

    async def delete_user(self, user_id: str) -> bool:
        # Clean up memberships so no "ghost" team member / node-operator rows
        # linger after the user row is gone.
        await self._db.execute("DELETE FROM team_members WHERE user_id = ?", (user_id,))
        await self._db.execute("DELETE FROM agent_users WHERE user_id = ?", (user_id,))
        return await self._db.execute_rowcount(
            "DELETE FROM users WHERE user_id = ?", (user_id,)
        ) == 1

    async def set_user_password(self, user_id: str, password_hash: str) -> bool:
        return await self._db.execute_rowcount(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (password_hash, user_id),
        ) == 1

    async def set_user_token(
        self,
        user_id: str,
        token_hash: str,
        token_created_at: datetime | None = None,
        token_expires_at: datetime | None = None,
    ) -> bool:
        return await self._db.execute_rowcount(
            "UPDATE users SET token_hash = ?, token_created_at = ?, token_expires_at = ? WHERE user_id = ?",
            (token_hash, token_created_at, token_expires_at, user_id),
        ) == 1

    async def set_user_last_login(
        self, user_id: str, last_login_at: datetime | None
    ) -> None:
        await self._db.execute(
            "UPDATE users SET last_login_at = ? WHERE user_id = ?",
            (last_login_at, user_id),
        )
