from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

from agent_mesh.shared.constants import TaskStatus

# ---------------------------------------------------------------------------
# Telemetry field registry (single source of truth for edge-reported fields)
#
# Every node self-description field reported over the heartbeat channel
# (`POST /api/edge/poll_for_task`) is registered here. The registry drives the
# store layer (column names are only ever taken from these constants, never
# interpolated from request data) and documents the persistence policy:
#
#   * SYSTEM_FIELDS : static facts. Missing -> keep the previously stored value
#                     (COALESCE semantics on UPDATE).
#   * METRIC_FIELDS : dynamic samples. Always overwritten each heartbeat.
#
# Fields are append-only: never rename or remove a registered field, and never
# add a new reporting endpoint. See docs/standards/edge-reporting.md for the
# full governance rules and the "add a field" checklist.
# ---------------------------------------------------------------------------
SYSTEM_FIELDS = ["os", "distro", "arch"]
METRIC_FIELDS = ["cpu_percent", "mem_percent", "mem_used_mb", "mem_total_mb"]
TELEMETRY_FIELDS = SYSTEM_FIELDS + METRIC_FIELDS


def extract_telemetry(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist-extract the telemetry subset of a heartbeat payload.

    Unknown keys are ignored (forward compatibility: a newer probe can send
    fields this orchestrator does not yet know). Values are passed through
    unchanged; a key that is absent is simply omitted.
    """
    return {f: payload.get(f) for f in TELEMETRY_FIELDS if f in payload}


def validate_telemetry(telemetry: Mapping[str, Any]) -> None:
    """Raise ValueError if telemetry contains any unregistered key."""
    unknown = set(telemetry) - set(TELEMETRY_FIELDS)
    if unknown:
        raise ValueError(f"unknown telemetry fields: {sorted(unknown)}")


class Constraints(BaseModel):
    workdir: str = "."
    timeout_s: int = 300
    model: str | None = None
    output_limit: int = 200_000
    session_id: str | None = None
    skills: list[str] | None = None


class ArtifactRef(BaseModel):
    filename: str
    size: int
    content_type: str
    download_url: str
    artifact_id: str


class FileRef(BaseModel):
    """Reference to a file from the file library, attached to a task.

    ``md5`` is captured at upload time; the edge verifies the downloaded bytes
    against it before executing the task.
    """
    file_id: str
    filename: str
    size: int
    content_type: str
    md5: str
    download_url: str


class TaskResult(BaseModel):
    status: Literal["completed", "failed"]
    mode: Literal["command", "llm"] = "llm"
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    duration_ms: int
    summary: str
    session_id: str | None = None


class Task(BaseModel):
    task_id: str
    agent_id: str
    mode: Literal["command", "llm"] = "llm"
    instruction: str
    constraints: Constraints = Field(default_factory=Constraints)
    status: TaskStatus = TaskStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    assigned_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retry_count: int = 0
    result: TaskResult | None = None
    max_retries: int = 0
    attachments: list[FileRef] = Field(default_factory=list)

    def model_dump_json_safe(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "mode": self.mode,
            "instruction": self.instruction,
            "constraints": self.constraints.model_dump(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "retry_count": self.retry_count,
            "result": self.result.model_dump() if self.result else None,
            "max_retries": self.max_retries,
            "attachments": [a.model_dump() for a in self.attachments],
        }


class AgentStatus(BaseModel):
    id: int
    agent_id: str
    device_id: str | None = None
    alias: str | None = None
    runtime: str | None = None
    hostname: str | None = None
    os: str | None = None
    distro: str | None = None
    arch: str | None = None
    version: str | None = None
    online: bool = False
    last_seen: datetime | None = None
    current_task_id: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    description: str | None = None
    effective_description: str | None = None
    system_prompt: str | None = None
    template_id: int | None = None
    template_name: str | None = None
    upgrade_requested: bool = False
    upgrade_version: str | None = None
    cpu_percent: float | None = None
    mem_percent: float | None = None
    mem_used_mb: float | None = None
    mem_total_mb: float | None = None

    def model_dump_json_safe(self) -> dict[str, Any]:
        display_name = self.alias or self.hostname or self.agent_id
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "device_id": self.device_id,
            "alias": self.alias,
            "display_name": display_name,
            "runtime": self.runtime,
            "hostname": self.hostname,
            "os": self.os,
            "distro": self.distro,
            "arch": self.arch,
            "version": self.version,
            "online": self.online,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "current_task_id": self.current_task_id,
            "llm_api_key": self.llm_api_key,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "description": self.description,
            "effective_description": self.effective_description,
            "system_prompt": self.system_prompt,
            "template_id": self.template_id,
            "template_name": self.template_name,
            "upgrade_requested": self.upgrade_requested,
            "upgrade_version": self.upgrade_version,
            "cpu_percent": self.cpu_percent,
            "mem_percent": self.mem_percent,
            "mem_used_mb": self.mem_used_mb,
            "mem_total_mb": self.mem_total_mb,
        }


class AgentDetail(AgentStatus):
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def model_dump_json_safe(self) -> dict[str, Any]:
        data = super().model_dump_json_safe()
        data.update({
            "tasks": self.tasks,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        })
        return data
