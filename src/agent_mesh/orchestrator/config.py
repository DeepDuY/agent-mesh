from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DB_PATH = "./data/agent-mesh.db"
DEFAULT_ARTIFACT_DIR = "./data/artifacts"
DEFAULT_SWEEP_INTERVAL_S = 5.0
DEFAULT_OFFLINE_AFTER_S = 15.0


def bootstrap_version(db_path: str) -> str | None:
    """Version of the published bootstrap package next to the DB, or None.

    The bootstrap package is stored at ``<db_dir>/bootstrap``; this is the
    single source of truth shared by the edge poll and the agent-upgrade routes.
    """
    version_path = Path(db_path).parent / "bootstrap" / "VERSION"
    try:
        value = version_path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


class OrchestratorConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_MESH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    # No hardcoded default: an empty token disables the "global" identity
    # entirely (require_any_token no longer matches), so an unconfigured
    # deployment is closed rather than open. Set AGENT_MESH_TOKEN in production.
    token: str = ""
    public_url: str = ""

    # Worker count. Only single-process is supported today; values >1 are
    # ignored and the server still runs one process (multi-worker needs a
    # shared realtime bus and atomic capacity checks, see docs/known-issues.md).
    workers: int = Field(
        default=1,
        description="uvicorn worker count. Only 1 is supported; >1 is ignored "
        "(runs single-process).",
    )

    # Database type
    db_type: Literal["sqlite", "pg"] = "sqlite"

    # SQLite (db_type=sqlite)
    db_path: str = DEFAULT_DB_PATH

    # PostgreSQL (db_type=pg)
    pg_dsn: str = ""
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_user: str = "agent_mesh"
    pg_password: str = ""
    pg_database: str = "agent_mesh"
    pg_min_connections: int = 2
    pg_max_connections: int = 10

    artifact_dir: str = DEFAULT_ARTIFACT_DIR
    artifact_max_size_mb: int = 50
    artifact_max_total_mb: int = 200
    sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S
    offline_after_s: float = DEFAULT_OFFLINE_AFTER_S
    session_ttl_s: int = 86400


def constant_time_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())
