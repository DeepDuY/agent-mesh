from __future__ import annotations

import os
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DB_PATH = "./data/agent-mesh.db"
DEFAULT_ARTIFACT_DIR = "./data/artifacts"
DEFAULT_SWEEP_INTERVAL_S = 5.0
DEFAULT_OFFLINE_AFTER_S = 15.0


class OrchestratorConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_MESH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    token: str = "change-me-shared-secret"
    public_url: str = ""

    # Multi-process
    workers: int = Field(
        default_factory=lambda: os.cpu_count() or 1,
        description="uvicorn worker count. 1=single-process, >1=multi-worker",
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


class EdgeConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EDGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    agent_id: str = Field(default_factory=lambda: os.uname().nodename)
    orchestrator_url: str = "http://127.0.0.1:8000"
    token: str = "change-me-shared-secret"
    heartbeat_s: float = 3.0
    runtime: str = "opencode"
    workdir: str = "."
    install_dir: str = "/opt/agent-mesh-agent"
    llm_api_key: str = ""
    llm_base_url: str = ""
    # No hardcoded model default: must be configured (or passed per task).
    llm_model: str = ""
    # Comma/newline separated available model ids (comma-joined when persisted
    # to edge.env as EDGE_LLM_MODELS).
    llm_models: str = ""
    # Node-level role/context injected at the top of every llm task prompt.
    system_prompt: str = ""


def constant_time_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())
