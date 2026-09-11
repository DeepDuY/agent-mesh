from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _pwd_context.verify(password, hashed)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 of an API token. Only the digest is stored in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def make_session_token(
    username: str,
    secret: str,
    ttl_s: int = 86400,
    now: float | None = None,
) -> str:
    """Issue a stateless, HMAC-signed session token (valid for `ttl_s` seconds).

    Multi-worker safe: no server-side session store is needed.
    """
    payload = {
        "sub": username,
        "exp": int((now if now is not None else time.time()) + ttl_s),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def verify_session_token(
    token: str,
    secret: str,
    now: float | None = None,
) -> str | None:
    """Validate a session token and return the embedded username, or None."""
    if not secret:
        # A session token cannot be verified without a signing secret; reject.
        return None
    try:
        body, sig = token.rsplit(".", 1)
        expected = _b64url_encode(
            hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, sig):
            return None
        payload: dict[str, Any] = json.loads(_b64url_decode(body))
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp < (now if now is not None else time.time()):
            return None
        username = payload.get("sub")
        return username if isinstance(username, str) else None
    except Exception:
        return None


async def resolve_token_user(store: Any, token: str) -> dict[str, Any] | None:
    """Resolve a Bearer token to a user dict.

    Accepts either a long-lived API token (SHA-256 lookup) or a signed session
    token issued by :func:`make_session_token`. Disabled users resolve to None.
    """
    user = await store.store.get_user_by_token_hash(hash_token(token))
    if user is None:
        secret = await store.store.get_setting("session_secret")
        if secret:
            username = verify_session_token(token, secret)
            if username:
                user = await store.store.get_user_by_username(username)
    if user is None or user.get("disabled"):
        return None
    return user
