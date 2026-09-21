"""Minimal cron expression support (5 fields, no external dependency).

Field order::

    minute hour day-of-month month day-of-week

Supported syntax per field: ``*``, ``a``, ``a-b``, ``a,b,c``, ``*/n`` and
``a-b/n``. Months are 1-12; day-of-week is 0-6 with Sunday=0 (7 is also accepted
as Sunday). Day-of-month and day-of-week combine the Vixie-cron way: when both
are restricted, a day matches if *either* matches; when only one is restricted,
only that one applies.

Times are evaluated in the schedule's timezone (wall clock) and returned as
UTC-aware datetimes.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FIELDS = 5
# Safety bound for the search loop. Five years covers Feb-29-only schedules.
_MAX_ITERATIONS = 5 * 366 * 24 * 60


def _parse_field(field: str, lo: int, hi: int) -> tuple[set[int], bool]:
    """Return (allowed values, wildcard?) for one cron field; raise on bad input."""
    field = field.strip()
    if not field:
        raise ValueError("empty cron field")
    wildcard = field == "*" or field.startswith("*/")
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"invalid cron field: {field!r}")
        step = 1
        rng = part
        if "/" in part:
            rng, step_raw = part.split("/", 1)
            if not step_raw.isdigit():
                raise ValueError(f"invalid step in cron field: {part!r}")
            step = int(step_raw)
            if step <= 0:
                raise ValueError(f"invalid step in cron field: {part!r}")
        if rng == "*":
            start, end = lo, hi
            if "/" not in part:
                wildcard = True
        elif "-" in rng:
            a, b = rng.split("-", 1)
            if not (a.lstrip("-").isdigit() and b.isdigit()):
                raise ValueError(f"invalid range in cron field: {part!r}")
            start, end = int(a), int(b)
        else:
            if not rng.isdigit():
                raise ValueError(f"invalid value in cron field: {part!r}")
            start = end = int(rng)
            if "/" in part:
                end = hi
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron field value out of range: {part!r}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"cron field matches nothing: {field!r}")
    return values, wildcard


def validate_cron(expr: str) -> None:
    """Raise ValueError if ``expr`` is not a valid 5-field cron expression."""
    fields = expr.split()
    if len(fields) != _FIELDS:
        raise ValueError("cron expression must have 5 fields (min hour dom month dow)")
    _parse_field(fields[0], 0, 59)
    _parse_field(fields[1], 0, 23)
    _parse_field(fields[2], 1, 31)
    _parse_field(fields[3], 1, 12)
    _parse_field(fields[4], 0, 7)


def _day_matches(
    dt: datetime,
    doms: set[int],
    dom_wild: bool,
    dows: set[int],
    dow_wild: bool,
) -> bool:
    dom_ok = dt.day in doms
    # isoweekday(): Mon=1..Sun=7 -> Sunday=0
    dow_ok = (dt.isoweekday() % 7) in dows
    if not dom_wild and not dow_wild:
        return dom_ok or dow_ok
    if not dom_wild:
        return dom_ok
    if not dow_wild:
        return dow_ok
    return True


def next_run_after(expr: str, after: datetime, tz: tzinfo | None = None) -> datetime:
    """Next cron firing strictly after ``after`` (aware UTC), as an aware UTC datetime."""
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    if tz is None:
        tz = timezone.utc
    fields = expr.split()
    if len(fields) != _FIELDS:
        raise ValueError("cron expression must have 5 fields (min hour dom month dow)")
    minutes, _ = _parse_field(fields[0], 0, 59)
    hours, _ = _parse_field(fields[1], 0, 23)
    doms, dom_wild = _parse_field(fields[2], 1, 31)
    months, _ = _parse_field(fields[3], 1, 12)
    dows_raw, dow_wild = _parse_field(fields[4], 0, 7)
    dows = {0 if d == 7 else d for d in dows_raw}

    cand = (after.astimezone(tz) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(_MAX_ITERATIONS):
        if cand.month not in months:
            if cand.month == 12:
                cand = cand.replace(year=cand.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                cand = cand.replace(month=cand.month + 1, day=1, hour=0, minute=0)
            continue
        if not _day_matches(cand, doms, dom_wild, dows, dow_wild):
            cand = (cand + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cand.hour not in hours:
            cand = (cand + timedelta(hours=1)).replace(minute=0)
            continue
        if cand.minute not in minutes:
            cand = cand + timedelta(minutes=1)
            continue
        return cand.astimezone(timezone.utc)
    raise ValueError(f"no matching time within range for cron: {expr!r}")


def resolve_timezone(name: str | None) -> tzinfo | None:
    """Resolve an IANA timezone name, or None if unknown/empty.

    ``name`` empty/None means "system default"; an unparseable non-empty name
    returns None so callers can warn and fall back.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return None
    env = (os.environ.get("TZ") or "").strip()
    if env:
        try:
            return ZoneInfo(env)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        resolved = os.path.realpath("/etc/localtime")
    except OSError:
        resolved = ""
    marker = "zoneinfo/"
    if marker in resolved:
        candidate = resolved.split(marker, 1)[1]
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    # Last resort: the process-local fixed offset (no DST rules, but usable).
    return datetime.now().astimezone().tzinfo or timezone.utc


def system_timezone_name() -> str:
    """Best-effort IANA name for the system timezone (for display/defaults)."""
    tz = resolve_timezone(None)
    key = getattr(tz, "key", None)
    if key:
        return key
    return "UTC"
