from __future__ import annotations

from typing import Any

MB = 1024 * 1024

# Integer settings that can be tuned from the Web console ("上传大小限制").
# key -> (default, min, max). Values are MB unless the key ends with ``_s``.
LIMIT_SPECS: dict[str, tuple[int, int, int]] = {
    "file_max_size_mb": (100, 1, 102400),
    "artifact_max_size_mb": (100, 1, 102400),
    "artifact_task_total_mb": (100, 1, 1048576),
    "artifact_total_mb": (200, 1, 1048576),
    "artifact_timeout_s": (300, 1, 86400),
    "artifact_evict_oldest": (1, 0, 1),
}


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    """Best-effort parse of a stored setting, clamped into ``[lo, hi]``."""
    value = _coerce_int(raw)
    if value is None:
        return default
    return max(lo, min(hi, value))


async def get_int_setting(store: Any, key: str) -> int:
    """Resolve an integer setting, falling back to its built-in default.

    Read per request so a change applies immediately and in every worker.
    """
    default, lo, hi = LIMIT_SPECS[key]
    raw = await store.store.get_setting(key)
    return clamp_int(raw, default, lo, hi)


def validate_limit(key: str, value: Any) -> str | None:
    """Return an error message when ``value`` is not a valid value for ``key``."""
    _default, lo, hi = LIMIT_SPECS[key]
    parsed = _coerce_int(value)
    if parsed is None:
        return f"{key} must be an integer"
    if not (lo <= parsed <= hi):
        return f"{key} must be between {lo} and {hi}"
    return None
