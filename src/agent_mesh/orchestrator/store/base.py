from __future__ import annotations

import abc
import uuid
from datetime import datetime
from typing import Any

from agent_mesh.shared.schemas import AgentStatus, Task, TaskResult


def _new_task_id() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


class AbstractStore(abc.ABC):
    """Abstract persistence layer for agent-mesh orchestrator."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def initialize(self) -> None:
        """Create tables / connections."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources."""

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def upsert_agent(
        self,
        agent_id: str,
        device_id: str | None,
        runtime: str | None,
        hostname: str | None,
        online: bool,
        last_seen: datetime | None,
        current_task_id: str | None,
        metadata: dict[str, Any] | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        version: str | None = None,
        telemetry: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> int: ...

    @abc.abstractmethod
    async def get_agent(self, device_id: str) -> AgentStatus | None: ...

    @abc.abstractmethod
    async def get_agent_by_agent_id(self, agent_id: str) -> AgentStatus | None: ...

    @abc.abstractmethod
    async def get_agent_by_id(self, agent_id: int) -> AgentStatus | None: ...

    @abc.abstractmethod
    async def list_agents(self) -> list[AgentStatus]: ...

    @abc.abstractmethod
    async def set_agent_online(self, device_id: str, online: bool) -> None: ...

    @abc.abstractmethod
    async def set_agent_current_task(
        self, device_id: str, task_id: str | None
    ) -> None: ...

    @abc.abstractmethod
    async def set_agent_alias_by_id(self, agent_id: int, alias: str | None) -> None: ...

    @abc.abstractmethod
    async def set_agent_description_by_id(
        self, agent_id: int, description: str | None
    ) -> None: ...

    @abc.abstractmethod
    async def set_agent_system_prompt_by_id(
        self, agent_id: int, system_prompt: str | None
    ) -> None: ...

    @abc.abstractmethod
    async def set_agent_template_by_id(
        self, agent_id: int, template_id: int | None
    ) -> None: ...

    @abc.abstractmethod
    async def delete_agent(self, agent_id: int) -> bool: ...

    @abc.abstractmethod
    async def set_agent_llm_config(
        self,
        agent_id: int,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def get_agent_token_hash(self, agent_id: int) -> str | None: ...

    @abc.abstractmethod
    async def set_agent_token_hash(self, agent_id: int, token_hash: str | None) -> None: ...

    @abc.abstractmethod
    async def get_agent_by_token_hash(self, token_hash: str) -> AgentStatus | None: ...

    @abc.abstractmethod
    async def add_agent_user(self, agent_id: int, user_id: str) -> None: ...

    @abc.abstractmethod
    async def list_agent_users(self, agent_id: int) -> list[str]: ...

    @abc.abstractmethod
    async def set_agent_access_by_id(
        self, agent_id: int, access: dict[str, Any] | None
    ) -> None: ...

    # ------------------------------------------------------------------
    # Teams / groups
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def create_team(
        self, team_id: str, name: str, description: str | None = None
    ) -> None: ...

    @abc.abstractmethod
    async def get_team(self, team_id: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def get_team_by_name(self, name: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def list_teams(self) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def update_team(
        self,
        team_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> bool: ...

    @abc.abstractmethod
    async def delete_team(self, team_id: str) -> bool: ...

    @abc.abstractmethod
    async def list_team_members(self, team_id: str) -> list[str]: ...

    @abc.abstractmethod
    async def set_user_team(self, user_id: str, team_id: str | None) -> None: ...

    @abc.abstractmethod
    async def get_user_team(self, user_id: str) -> str | None: ...

    @abc.abstractmethod
    async def request_agent_upgrade(self, agent_id: int, version: str) -> bool: ...

    @abc.abstractmethod
    async def clear_agent_upgrade(self, agent_id: int) -> None: ...

    # ------------------------------------------------------------------
    # Node templates
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def create_template(
        self,
        name: str,
        description: str | None = None,
        node_description: str | None = None,
        system_prompt: str | None = None,
        llm_model: str | None = None,
        permission: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> int: ...

    @abc.abstractmethod
    async def get_template(self, template_id: int) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def get_template_by_name(self, name: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def list_templates(self) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def update_template(
        self, template_id: int, **fields: Any
    ) -> bool: ...

    @abc.abstractmethod
    async def delete_template(self, template_id: int) -> bool: ...

    # ------------------------------------------------------------------
    # Skills library
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def upsert_skill(
        self,
        name: str,
        description: str,
        version: int,
        enabled: bool,
        filename: str,
    ) -> None: ...

    @abc.abstractmethod
    async def get_skill(self, name: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def list_skills(self) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def update_skill(
        self,
        name: str,
        description: str | None = None,
        version: int | None = None,
        enabled: bool | None = None,
        filename: str | None = None,
    ) -> bool: ...

    @abc.abstractmethod
    async def delete_skill(self, name: str) -> bool: ...

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def create_task(
        self,
        task: Task,
        dispatched_by: str | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def get_task(self, task_id: str) -> Task | None: ...

    @abc.abstractmethod
    async def list_tasks(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        search: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> list[Task]: ...

    @abc.abstractmethod
    async def count_tasks(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        search: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> int: ...

    @abc.abstractmethod
    async def delete_task(self, task_id: str) -> bool: ...

    @abc.abstractmethod
    async def delete_tasks(self, task_ids: list[str]) -> int: ...

    @abc.abstractmethod
    async def update_task_status(
        self,
        task_id: str,
        status: str,
        assigned_at: datetime | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        retry_count: int | None = None,
        expected_status: str | None = None,
    ) -> bool: ...

    @abc.abstractmethod
    async def set_task_result(
        self, task_id: str, result: TaskResult
    ) -> bool: ...

    @abc.abstractmethod
    async def append_task_event(
        self,
        task_id: str,
        event_type: str,
        agent_id: str | None = None,
        user_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def list_task_events(
        self, task_id: str, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Task queue
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def enqueue(self, task_id: str, agent_id: str) -> None: ...

    @abc.abstractmethod
    async def dequeue(self, agent_id: str) -> str | None: ...

    @abc.abstractmethod
    async def remove_from_queue(self, task_id: str) -> None: ...

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def save_artifact(
        self,
        task_id: str,
        artifact_id: str,
        filename: str,
        size: int,
        content_type: str,
        storage_path: str,
    ) -> None: ...

    @abc.abstractmethod
    async def list_artifacts(self, task_id: str) -> list[dict[str, Any]]: ...

    # ------------------------------------------------------------------
    # Task logs (live execution stream)
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def append_task_logs(
        self, task_id: str, entries: list[dict[str, str]]
    ) -> None: ...

    @abc.abstractmethod
    async def list_task_logs(
        self, task_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def delete_task_logs(self, task_id: str) -> None: ...

    # ------------------------------------------------------------------
    # File library
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def create_file(
        self,
        file_id: str,
        filename: str,
        size: int,
        content_type: str,
        md5: str,
        created_by: str | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def get_file(self, file_id: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def get_files_by_ids(self, file_ids: list[str]) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def find_file_by_md5(
        self, md5: str, filename: str, owner: str | None = None
    ) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def list_files(
        self, search: str | None = None, owner: str | None = None
    ) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def delete_file(self, file_id: str) -> bool: ...

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @abc.abstractmethod
    async def get_setting(self, key: str) -> str | None: ...
    @abc.abstractmethod
    async def set_setting(self, key: str, value: str) -> None: ...

    @abc.abstractmethod
    async def list_settings(self) -> dict[str, str]: ...

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    @abc.abstractmethod
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
    ) -> None: ...

    @abc.abstractmethod
    async def get_user_by_username(self, username: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def get_user_by_token_hash(
        self, token_hash: str
    ) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    async def list_users(self) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def delete_user(self, user_id: str) -> bool: ...

    @abc.abstractmethod
    async def set_user_password(
        self, user_id: str, password_hash: str
    ) -> bool: ...

    @abc.abstractmethod
    async def set_user_token(
        self,
        user_id: str,
        token_hash: str,
        token_created_at: datetime | None = None,
    ) -> bool: ...

    @abc.abstractmethod
    async def set_user_last_login(
        self, user_id: str, last_login_at: datetime | None
    ) -> None: ...
