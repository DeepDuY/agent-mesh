"""Post-startup database schema verification.

Runs after migrations / column-level upgrades so a partially-applied upgrade
fails fast at startup instead of surfacing later as runtime 500s (a missing
column breaks a query long after the "successful" deploy). Read-only: it only
introspects the live schema and reports what is missing.

The required set is intentionally small and focused on the upgrade-sensitive
changes (migrations 023/024/025 + multi-team ownership). Base tables/columns are
created by every code path, so checking the recent ones is enough to catch a
skipped or half-applied migration without duplicating the whole schema.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_TABLES = frozenset(
    {
        "schema_migrations",
        "tasks",
        "task_reviews",
        "task_events",
        "idempotency_keys",
    }
)

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "tasks": frozenset({"user_id", "team_ids", "run_id", "review_score", "review_reason"}),
    "schedules": frozenset({"team_ids"}),
    "templates": frozenset({"owner_team_ids"}),
}

# Same directory the SQLite migration runner reads (store/migrations).
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _expected_sqlite_versions() -> set[int]:
    versions: set[int] = set()
    if not _MIGRATIONS_DIR.is_dir():
        return versions
    for f in _MIGRATIONS_DIR.glob("*.sql"):
        head = f.stem.split("_", 1)[0]
        if head.isdigit():
            versions.add(int(head))
    return versions


async def _table_names(db: Any, backend: str) -> set[str]:
    if backend == "pg":
        rows = await db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
        )
        return {r["table_name"] for r in rows}
    rows = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r["name"] for r in rows}


async def _column_names(db: Any, backend: str, table: str) -> set[str]:
    if backend == "pg":
        rows = await db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_name = ?",
            (table,),
        )
        return {r["column_name"] for r in rows}
    rows = await db.execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in rows}


async def verify_schema(db: Any, backend: str) -> dict[str, Any]:
    """Introspect the live schema and report whether it matches the code.

    Returns a status dict; never raises for a merely incomplete schema (the
    caller decides whether to abort startup). Fields:

    * ``ok`` -- no missing tables/columns and no unapplied migrations
    * ``backend`` -- ``"sqlite"`` or ``"pg"``
    * ``schema_version`` -- max applied version (SQLite only, else ``None``)
    * ``missing_tables`` / ``missing_columns`` / ``pending_migrations``
    """
    backend = "pg" if backend == "pg" else "sqlite"
    # Accept either the connection layer (Database) or a store object that wraps
    # one in ``_db`` (SQLiteStore / PostgresStore).
    db = getattr(db, "_db", db)
    tables = await _table_names(db, backend)

    missing_tables = sorted(t for t in _REQUIRED_TABLES if t not in tables)
    missing_columns: dict[str, list[str]] = {}
    for table, cols in _REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        have = await _column_names(db, backend, table)
        miss = sorted(c for c in cols if c not in have)
        if miss:
            missing_columns[table] = miss

    schema_version: int | None = None
    pending_migrations: list[int] = []
    if backend == "sqlite":
        row = await db.fetchrow("SELECT MAX(version) AS v FROM schema_migrations")
        if row is not None:
            schema_version = row["v"]
        applied: set[int] = set()
        if "schema_migrations" in tables:
            rows = await db.execute("SELECT version FROM schema_migrations")
            applied = {r["version"] for r in rows}
        pending_migrations = sorted(_expected_sqlite_versions() - applied)

    ok = not missing_tables and not missing_columns and not pending_migrations
    status: dict[str, Any] = {
        "ok": ok,
        "backend": backend,
        "schema_version": schema_version,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "pending_migrations": pending_migrations,
    }
    if not ok:
        logger.error("database schema verification failed: %s", status)
    return status
