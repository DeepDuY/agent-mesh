"""Unified database connection layer.

Split into cohesive modules:

* :mod:`base`      -- ``Database`` abstract + shared value helpers
* :mod:`sqlite`    -- ``SQLiteDatabase``
* :mod:`pg`        -- ``PostgresDatabase`` + ``?`` -> ``$n`` placeholder rewrite
* :mod:`pg_schema` -- PostgreSQL schema bootstrap / column-level upgrades

The public names are re-exported here so existing imports from
``agent_mesh.orchestrator.store.connection`` keep working.
"""

from agent_mesh.orchestrator.store.connection.base import (
    Database,
    _dump_json,
    _dt_to_iso,
    _iso_to_dt,
    _load_json,
    _pg_param,
    _utcnow,
)
from agent_mesh.orchestrator.store.connection.pg import (
    PostgresDatabase,
    _rewrite_placeholders,
)
from agent_mesh.orchestrator.store.connection.sqlite import SQLiteDatabase

__all__ = [
    "Database",
    "SQLiteDatabase",
    "PostgresDatabase",
    "_dt_to_iso",
    "_iso_to_dt",
    "_load_json",
    "_dump_json",
    "_utcnow",
    "_pg_param",
    "_rewrite_placeholders",
]
