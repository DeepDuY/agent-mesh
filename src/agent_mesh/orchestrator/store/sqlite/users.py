from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_mesh.orchestrator.store.sqlite.connection import _dt_to_iso, SQLiteBase

_USER_COLUMNS = (
    "user_id, username, password_hash, role, disabled, created_by, "
    "created_at, last_login_at, token_created_at, token_expires_at, token_hash"
)


def _row_to_user(row: Any) -> dict[str, Any]:
    return dict(row)


class UsersMixin(SQLiteBase):
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
        await self._execute(
            """
            INSERT INTO users (user_id, username, password_hash, token_hash, role, disabled, created_by, token_created_at, token_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                password_hash,
                token_hash,
                role,
                1 if disabled else 0,
                created_by,
                _dt_to_iso(token_created_at),
                _dt_to_iso(token_expires_at),
            ),
        )

    async def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        rows = await self._execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?", (username,)
        )
        return _row_to_user(rows[0]) if rows else None

    async def get_user_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        rows = await self._execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE token_hash = ?", (token_hash,)
        )
        return _row_to_user(rows[0]) if rows else None

    async def list_users(self) -> list[dict[str, Any]]:
        rows = await self._execute(
            f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at ASC"
        )
        return [_row_to_user(row) for row in rows]

    async def delete_user(self, user_id: str) -> bool:
        # Clean up memberships so no "ghost" team member / node-operator rows
        # linger after the user row is gone.
        await self._execute("DELETE FROM team_members WHERE user_id = ?", (user_id,))
        await self._execute("DELETE FROM agent_users WHERE user_id = ?", (user_id,))
        count = await self._execute_rowcount(
            "DELETE FROM users WHERE user_id = ?", (user_id,)
        )
        return count > 0

    async def set_user_password(self, user_id: str, password_hash: str) -> bool:
        count = await self._execute_rowcount(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (password_hash, user_id),
        )
        return count > 0

    async def set_user_token(
        self,
        user_id: str,
        token_hash: str,
        token_created_at: datetime | None = None,
        token_expires_at: datetime | None = None,
    ) -> bool:
        count = await self._execute_rowcount(
            "UPDATE users SET token_hash = ?, token_created_at = ?, token_expires_at = ? WHERE user_id = ?",
            (
                token_hash,
                _dt_to_iso(token_created_at),
                _dt_to_iso(token_expires_at),
                user_id,
            ),
        )
        return count > 0

    async def set_user_last_login(
        self, user_id: str, last_login_at: datetime | None
    ) -> None:
        await self._execute(
            "UPDATE users SET last_login_at = ? WHERE user_id = ?",
            (_dt_to_iso(last_login_at), user_id),
        )
