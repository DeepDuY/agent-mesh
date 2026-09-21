"""Cron expression parsing and next-run calculation (no external dependency)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent_mesh.orchestrator.cron import (
    next_run_after,
    resolve_timezone,
    system_timezone_name,
    validate_cron,
)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_every_minute():
    assert next_run_after("* * * * *", _utc(2026, 1, 1, 0, 0, 30)) == _utc(2026, 1, 1, 0, 1)


def test_step_minutes():
    assert next_run_after("*/15 * * * *", _utc(2026, 1, 1, 0, 7)) == _utc(2026, 1, 1, 0, 15)


def test_daily_in_timezone():
    tz = ZoneInfo("Asia/Shanghai")
    # 00:00 UTC is 08:00 in Shanghai; next 09:30 local is 01:30 UTC.
    assert next_run_after("30 9 * * *", _utc(2026, 1, 1, 0, 0), tz) == _utc(2026, 1, 1, 1, 30)


def test_dom_and_dow_or_semantics():
    # "1st of month OR Monday" (Vixie cron). 2026-01-01 is a Thursday, so the
    # next match after Jan 1 00:00 is Jan 5 (Monday).
    assert next_run_after("0 0 1 * 1", _utc(2026, 1, 1, 0, 0)) == _utc(2026, 1, 5)


def test_sunday_zero_and_seven():
    # 2026-01-04 is a Sunday.
    assert next_run_after("0 12 * * 0", _utc(2026, 1, 1, 0, 0)) == _utc(2026, 1, 4, 12)
    assert next_run_after("0 12 * * 7", _utc(2026, 1, 1, 0, 0)) == _utc(2026, 1, 4, 12)


def test_lists_and_ranges():
    assert next_run_after("0 9,17 * * *", _utc(2026, 1, 1, 10, 0)) == _utc(2026, 1, 1, 17)
    assert next_run_after("0 9-17 * * *", _utc(2026, 1, 1, 18, 0)) == _utc(2026, 1, 2, 9)


@pytest.mark.parametrize(
    "expr",
    ["", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "x * * * *"],
)
def test_invalid_expressions(expr):
    with pytest.raises(ValueError):
        validate_cron(expr)


def test_validate_accepts_valid():
    validate_cron("0 3 * * 1-5")
    validate_cron("*/5 9-18 1,15 * *")


def test_resolve_timezone():
    assert resolve_timezone("UTC") is not None
    assert resolve_timezone("Not/AZone") is None  # invalid -> None
    assert resolve_timezone(None) is not None  # system fallback
    assert isinstance(system_timezone_name(), str)
