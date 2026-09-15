from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_mesh.orchestrator.store.base import AbstractStore, _new_task_id
from agent_mesh.shared.constants import (
    DEFAULT_OFFLINE_AFTER_S,
    DEFAULT_SWEEP_INTERVAL_S,
    TaskStatus,
)
from agent_mesh.shared.schemas import (
    AgentStatus,
    Constraints,
    FileRef,
    Task,
    TaskResult,
    validate_telemetry,
)

logger = logging.getLogger(__name__)


class TaskStore:
    """Task store + state machine + heartbeat registry backed by an AbstractStore."""

    def __init__(
        self,
        store: AbstractStore,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        offline_after_s: float = DEFAULT_OFFLINE_AFTER_S,
    ):
        self.store = store
        self._sweep_interval_s = sweep_interval_s
        self._offline_after_s = offline_after_s
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public read helpers
    # ------------------------------------------------------------------
    async def get_task(self, task_id: str) -> Task | None:
        return await self.store.get_task(task_id)

    async def list_tasks(
        self,
        agent_id: str | None = None,
        status: TaskStatus | None = None,
        mode: str | None = None,
        search: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        owner_user_id: str | None = None,
        owner_team_id: str | None = None,
    ) -> list[Task]:
        return await self.store.list_tasks(
            agent_id=agent_id,
            status=status.value if status else None,
            mode=mode,
            search=search,
            started_after=started_after,
            started_before=started_before,
            limit=limit,
            offset=offset,
            owner_user_id=owner_user_id,
            owner_team_id=owner_team_id,
        )

    async def list_agents(self) -> list[AgentStatus]:
        return await self.store.list_agents()

    async def describe_agents(self, agents: list[AgentStatus]) -> None:
        """Fill display-only fields: ``template_name`` and ``effective_description``.

        Effective description precedence: node's own ``description`` > the bound
        template's ``node_description``. The template's own ``description`` (its
        human-facing note) is NOT used here. Templates are admin-only elsewhere,
        but these labels are safe to surface to the main agent.
        """
        if not agents:
            return
        templates = {t["id"]: t for t in await self.store.list_templates()}
        for a in agents:
            tpl = templates.get(a.template_id) if a.template_id else None
            a.template_name = tpl.get("name") if tpl else None
            tpl_desc = ((tpl.get("node_description") if tpl else "") or "").strip() or None
            node_desc = (a.description or "").strip() or None
            a.effective_description = node_desc or tpl_desc

    # ------------------------------------------------------------------
    # Tenancy / access control
    # ------------------------------------------------------------------
    @staticmethod
    def is_admin(user: dict[str, Any] | None) -> bool:
        return bool(user) and user.get("role") == "admin"

    async def user_team(self, user: dict[str, Any] | None) -> str | None:
        user_id = (user or {}).get("user_id")
        if not user_id:
            return None
        return await self.store.get_user_team(user_id)

    async def can_access_agent(self, user: dict[str, Any] | None, agent) -> bool:
        """Whether ``user`` may operate ``agent`` (admin always may)."""
        if self.is_admin(user):
            return True
        if agent is None:
            return False
        access = agent.access or {}
        user_id = (user or {}).get("user_id")
        if user_id and user_id in (access.get("users") or []):
            return True
        team = await self.user_team(user)
        return bool(team and team in (access.get("teams") or []))

    async def accessible_agent_ids(
        self, user: dict[str, Any] | None
    ) -> set[int] | None:
        """Numeric ids the user may access, or None for admin (all)."""
        if self.is_admin(user):
            return None
        user_id = (user or {}).get("user_id")
        team = await self.user_team(user)
        ids: set[int] = set()
        for a in await self.store.list_agents():
            access = a.access or {}
            if (user_id and user_id in (access.get("users") or [])) or (
                team and team in (access.get("teams") or [])
            ):
                ids.add(a.id)
        return ids

    async def grant_agent_access(
        self,
        agent,
        *,
        user_id: str | None = None,
        team_id: str | None = None,
    ) -> bool:
        """Add a user and/or team to a node's access list. Returns True if changed."""
        access = dict(agent.access or {})
        users = set(access.get("users") or [])
        teams = set(access.get("teams") or [])
        changed = False
        if user_id and user_id not in users:
            users.add(user_id)
            changed = True
        if team_id and team_id not in teams:
            teams.add(team_id)
            changed = True
        if changed:
            access["users"] = sorted(users)
            access["teams"] = sorted(teams)
            await self.store.set_agent_access_by_id(agent.id, access)
            agent.access = access
        return changed

    async def can_see_task(self, user: dict[str, Any] | None, task) -> bool:
        """Whether a user identity may see ``task`` (admin always may).

        Visibility = own task (``user_id``) OR same team (``team_id``).
        """
        if self.is_admin(user):
            return True
        if task is None:
            return False
        user_id = (user or {}).get("user_id")
        if user_id and task.user_id == user_id:
            return True
        team = await self.user_team(user)
        return bool(team and task.team_id == team)

    async def edge_can_access_task(self, auth: dict[str, Any] | None, task) -> bool:
        """Whether an edge identity may read/write ``task``.

        - ``global`` token: trusted (legacy bootstrap).
        - ``user`` token: same tenancy visibility as the REST API.
        - ``agent`` token: the task must be queued on that agent's stable key
          (``device_id`` or ``agent_id``) — an agent may only touch its own tasks.
        """
        kind = (auth or {}).get("auth")
        if kind == "global":
            return True
        if task is None:
            return False
        if kind == "user":
            return await self.can_see_task(auth, task)
        if kind == "agent":
            agent = await self.store.get_agent_by_id(auth.get("agent_id"))
            key = (agent.device_id or agent.agent_id) if agent else None
            return bool(key and task.agent_id == key)
        return False

    async def ensure_agent_token(self, agent_id: int) -> str | None:
        """Issue a per-agent independent token on first registration.

        Returns the plaintext token exactly once (only when it is newly
        generated); subsequent calls return None. The hash is stored in the DB,
        so the agent token does not depend on any user login session.
        """
        existing = await self.store.get_agent_token_hash(agent_id)
        if existing:
            return None
        from agent_mesh.orchestrator.auth import generate_token, hash_token

        token = generate_token()
        await self.store.set_agent_token_hash(agent_id, hash_token(token))
        logger.info("issued new independent token for agent id=%s", agent_id)
        return token

    async def bump_config_version(self) -> None:
        """Increment the global LLM config version so edges re-sync their config."""
        current = await self.store.get_setting("config_version")
        try:
            version = int(current or 0)
        except ValueError:
            version = 0
        await self.store.set_setting("config_version", str(version + 1))

    async def resolve_agent(self, agent_ref: str) -> AgentStatus | None:
        return await self._resolve_agent(agent_ref)

    # ------------------------------------------------------------------
    # LLM model resolution / validation
    # ------------------------------------------------------------------
    async def allowed_models(self) -> list[str]:
        """Configured model allow-list (empty = no restriction)."""
        raw = await self.store.get_setting("llm_models") or ""
        return [m.strip() for m in re.split(r"[,\n]", raw) if m.strip()]

    async def resolve_llm_model(
        self, agent: AgentStatus | None, explicit: str | None
    ) -> str:
        """Effective model: explicit > node > bound template > global default."""
        if explicit:
            return explicit
        if agent is not None:
            if agent.llm_model:
                return agent.llm_model
            if agent.template_id:
                template = await self.store.get_template(agent.template_id)
                if template and template.get("llm_model"):
                    return template["llm_model"]
        return (await self.store.get_setting("llm_model")) or ""

    async def validate_llm_model(
        self, agent_ref: str, mode: str, explicit: str | None
    ) -> str | None:
        """Return an error message when an llm dispatch cannot proceed, else None.

        Enforces the force-configured policy (no hardcoded fallback model) and,
        when an allow-list is configured, that an explicit `model` is on it.
        """
        if mode != "llm":
            return None
        agent = await self._resolve_agent(agent_ref)
        resolved = await self.resolve_llm_model(agent, explicit)
        if not resolved:
            return "no LLM model configured: pass model or set a default model"
        allowed = await self.allowed_models()
        if explicit and allowed and explicit not in allowed:
            return f"model not in allowed list: {explicit}"
        return None

    # ------------------------------------------------------------------
    # Permission (template > global default > built-in readonly)
    # ------------------------------------------------------------------
    async def effective_permission(
        self, agent: AgentStatus | None, template: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Resolve the permission object governing a node.

        Permission is a template-level capability (no node/task override):
        bound template > global ``default_permission`` setting > built-in readonly.
        """
        from agent_mesh.shared import permissions

        if template is not None and template.get("permission"):
            return template["permission"]
        permission = permissions.loads(
            await self.store.get_setting("default_permission")
        )
        return permission if permission is not None else permissions.default_permission()

    async def check_command_permission(
        self, agent_ref: str, instruction: str
    ) -> str | None:
        """Return a denial reason when a command task is not permitted, else None.

        Server-side pre-check for immediate feedback; the edge re-evaluates as the
        enforcement point. Returns None when the agent is unknown (the normal
        "agent not found" path handles that).
        """
        from agent_mesh.shared import permissions

        agent = await self._resolve_agent(agent_ref)
        if agent is None:
            return None
        template = (
            await self.store.get_template(agent.template_id)
            if agent.template_id
            else None
        )
        permission = await self.effective_permission(agent, template)
        if permissions.evaluate_command(permission, instruction) != "allow":
            return "command denied by permission policy"
        return None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def dispatch(
        self,
        agent_id: str,
        instruction: str,
        mode: str = "llm",
        constraints: Constraints | None = None,
        max_retries: int = 0,
        depends_on: list[str] | None = None,
        dispatched_by: str | None = None,
        attachments: list[FileRef] | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
    ) -> str:
        if mode not in ("command", "llm"):
            raise ValueError(f"invalid task mode: {mode}")
        if depends_on:
            for dep_id in depends_on:
                if await self.get_task(dep_id) is None:
                    raise ValueError(f"dependency task not found: {dep_id}")

        # Resolve the target agent. agent_id may be numeric id, device_id, or legacy string.
        target = await self._resolve_agent(agent_id)
        if target is None:
            raise ValueError(f"agent not found: {agent_id}")
        queue_key = target.device_id or target.agent_id

        task = Task(
            task_id=_new_task_id(),
            agent_id=queue_key,
            mode=mode,  # type: ignore[arg-type]
            instruction=instruction,
            constraints=constraints or Constraints(),
            status=TaskStatus.QUEUED,
            max_retries=max_retries,
            attachments=attachments or [],
            user_id=user_id,
            team_id=team_id,
        )
        await self.store.create_task(
            task, dispatched_by=dispatched_by, user_id=user_id, team_id=team_id
        )
        await self.store.enqueue(task.task_id, queue_key)
        logger.info("dispatched task %s for agent=%s mode=%s", task.task_id, queue_key, mode)
        return task.task_id

    async def _resolve_agent(self, agent_ref: str) -> AgentStatus | None:
        """Resolve an agent reference: numeric id, device_id (machine-id), or agent_id string."""
        # Try numeric id first.
        try:
            numeric_id = int(agent_ref)
            agent = await self.store.get_agent_by_id(numeric_id)
            if agent:
                return agent
        except ValueError:
            pass
        # Try device_id directly.
        agent = await self.store.get_agent(agent_ref)
        if agent:
            return agent
        # Fall back to agent_id string; return the most recently updated match.
        agents = await self.store.list_agents()
        matches = [a for a in agents if a.agent_id == agent_ref]
        if matches:
            matches.sort(key=lambda a: a.last_seen or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return matches[0]
        return None

    async def _agent_stable_key(self, agent_ref: str) -> str:
        """Resolve an agent ref to its current stable key (device_id or agent_id).

        Mirrors the key selection in :meth:`heartbeat` so clearing ``current_task_id``
        always targets the same agent row that claimed the task.
        """
        agent = await self._resolve_agent(agent_ref)
        if agent is not None:
            return agent.device_id or agent.agent_id
        return agent_ref

    # ------------------------------------------------------------------
    # Heartbeat / claim
    # ------------------------------------------------------------------
    async def list_active_tasks(self, agent_key: str) -> list[Task]:
        """All tasks currently assigned/working on an agent (single query list)."""
        active: list[Task] = []
        for status in (TaskStatus.ASSIGNED, TaskStatus.WORKING):
            active.extend(
                await self.store.list_tasks(
                    agent_id=agent_key, status=status.value, limit=1000
                )
            )
        return active

    async def _refresh_current_task(self, agent_key: str) -> str | None:
        """Re-point ``agents.current_task_id`` at a task still active on the agent.

        ``current_task_id`` is a single legacy column, but agents execute several
        tasks concurrently (v1.4.0). On any terminal transition (submit/cancel/
        timeout/offline-requeue) recompute it so it keeps reflecting live work:
        prefer the first still-WORKING task, else the first ASSIGNED, else clear.
        This stops a finishing sibling task from wiping the display of one that
        is still running.
        """
        active = await self.list_active_tasks(agent_key)
        working = [t.task_id for t in active if t.status == TaskStatus.WORKING]
        chosen = None
        if working:
            chosen = min(working)
        elif active:
            chosen = min(t.task_id for t in active)
        await self.store.set_agent_current_task(agent_key, chosen)
        return chosen

    async def heartbeat(
        self,
        agent_id: str,
        device_id: str | None,
        runtime: str | None = None,
        hostname: str | None = None,
        version: str | None = None,
        telemetry: dict[str, Any] | None = None,
        running_tasks: list[str] | None = None,
    ) -> tuple[list[Task], str]:
        """Register/re-fresh the agent on heartbeat and claim queued tasks.

        Multi-task aware: ``running_tasks`` is the set of task ids the edge is
        currently executing. Tasks that are assigned/working but NOT in that set
        are returned as re-dispatch targets (edge restart recovery). New queued
        tasks are claimed up to the agent's ``max_concurrent`` cap.

        ``telemetry`` is the registered SYSTEM/METRIC field subset (see
        ``shared.schemas``); unregistered keys raise ``ValueError``.
        Returns ``(tasks_to_run, agent_key)`` where tasks_to_run is ordered
        ``[resume..., new...]``.
        """
        telemetry = dict(telemetry or {})
        validate_telemetry(telemetry)
        now = datetime.now(timezone.utc)
        key = device_id or agent_id
        agent = await self.store.get_agent(key)
        current_task_id = agent.current_task_id if agent else None

        await self.store.upsert_agent(
            agent_id=agent_id,
            device_id=device_id,
            runtime=runtime,
            hostname=hostname,
            version=version,
            online=True,
            last_seen=now,
            current_task_id=current_task_id,
            telemetry=telemetry,
        )

        active = await self.list_active_tasks(key)
        running = set(running_tasks or [])
        resume = [t for t in active if t.task_id not in running]

        # Claim new tasks up to the concurrency cap.
        max_concurrent = await self.max_concurrent()
        capacity = max(0, max_concurrent - len(active))
        new_tasks: list[Task] = []
        while capacity > 0:
            task_id = await self.store.dequeue(key)
            if not task_id:
                break
            task = await self.store.get_task(task_id)
            if not task or task.status != TaskStatus.QUEUED:
                continue
            await self.store.update_task_status(
                task_id=task_id,
                status=TaskStatus.ASSIGNED.value,
                assigned_at=now,
            )
            capacity -= 1
            new_tasks.append(task)
            logger.info("claimed task %s by agent=%s", task_id, key)

        # Keep current_task_id pointing at a live task (multi-task aware): with
        # several concurrent tasks the single legacy column must not flip to the
        # last-claimed task nor go blank while siblings still run.
        await self._refresh_current_task(key)

        return resume + new_tasks, key

    async def max_concurrent(self) -> int:
        value = await self.store.get_setting("max_concurrent")
        try:
            return max(1, int(value or 2))
        except ValueError:
            return 2

    async def mark_started(self, task_id: str) -> bool:
        now = datetime.now(timezone.utc)
        task = await self.store.get_task(task_id)
        if not task or task.status != TaskStatus.ASSIGNED:
            return False
        await self.store.update_task_status(
            task_id=task_id,
            status=TaskStatus.WORKING.value,
            started_at=now,
        )
        return True

    async def submit_result(self, task_id: str, result: TaskResult) -> bool:
        now = datetime.now(timezone.utc)
        task = await self.store.get_task(task_id)
        if not task or task.status not in (
            TaskStatus.ASSIGNED,
            TaskStatus.WORKING,
        ):
            return False
        await self.store.set_task_result(task_id, result)
        await self.store.update_task_status(
            task_id=task_id,
            status=result.status,
            finished_at=now,
        )
        # Clear the agent's current_task only if no sibling task is still active
        # (multi-task aware: a finishing sibling must not blank the running one).
        key = await self._agent_stable_key(task.agent_id)
        await self._refresh_current_task(key)
        return True

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task. Returns False if the task is already in a terminal state."""
        now = datetime.now(timezone.utc)
        task = await self.store.get_task(task_id)
        if not task:
            return False
        if task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED,
        ):
            return False
        if task.status == TaskStatus.QUEUED:
            await self.store.remove_from_queue(task_id)
        # assigned/working: the edge agent will observe the CANCELLED status on its
        # next poll and terminate the running subprocess (see edge/agent.py).
        await self.store.update_task_status(
            task_id=task_id,
            status=TaskStatus.CANCELLED.value,
            finished_at=now,
        )
        key = await self._agent_stable_key(task.agent_id)
        await self._refresh_current_task(key)
        logger.info("cancelled task %s (was %s)", task_id, task.status.value)
        return True

    # ------------------------------------------------------------------
    # Background sweepers
    # ------------------------------------------------------------------
    async def start_sweepers(self) -> None:
        self._stop_event.clear()
        asyncio.create_task(self._sweep_timeouts())
        asyncio.create_task(self._sweep_offline())

    async def stop_sweepers(self) -> None:
        self._stop_event.set()

    async def _sweep_timeouts(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._sweep_interval_s
                )
            except asyncio.TimeoutError:
                pass
            now = datetime.now(timezone.utc)
            tasks = await self.store.list_tasks(status=TaskStatus.WORKING.value, limit=1000)
            for stale in tasks:
                # Re-fetch so a task that finished while we were scanning is not
                # clobbered by a stale timeout decision.
                task = await self.store.get_task(stale.task_id)
                if not task or task.status != TaskStatus.WORKING:
                    continue
                if not task.started_at:
                    continue
                deadline = task.started_at + timedelta(
                    seconds=task.constraints.timeout_s
                )
                if now < deadline:
                    continue
                # Timed out. Atomically transition working -> timed_out so a
                # result submitted concurrently by the edge (which also flips
                # the status) cannot be clobbered by a stale sweep decision.
                result = TaskResult(
                    status="failed",
                    exit_code=-1,
                    stdout_tail="",
                    stderr_tail="task timed out",
                    duration_ms=int((now - task.started_at).total_seconds() * 1000),
                    summary="timed out",
                )
                marked = await self.store.update_task_status(
                    task_id=task.task_id,
                    status=TaskStatus.TIMED_OUT.value,
                    finished_at=now,
                    expected_status=TaskStatus.WORKING.value,
                )
                if not marked:
                    # Task already moved on (completed/failed/cancelled); do not
                    # overwrite its real result.
                    continue
                await self.store.set_task_result(task.task_id, result)
                await self._refresh_current_task(
                    await self._agent_stable_key(task.agent_id)
                )

                if task.retry_count < task.max_retries:
                    next_retry = task.retry_count + 1
                    await self.store.update_task_status(
                        task_id=task.task_id,
                        status=TaskStatus.QUEUED.value,
                        assigned_at=None,
                        started_at=None,
                        finished_at=None,
                        retry_count=next_retry,
                    )
                    await self.store.enqueue(task.task_id, task.agent_id)
                    logger.info(
                        "retrying task %s (attempt %d/%d)",
                        task.task_id,
                        next_retry,
                        task.max_retries,
                    )
                else:
                    logger.info("task %s timed out", task.task_id)

    async def _sweep_offline(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._sweep_interval_s
                )
            except asyncio.TimeoutError:
                pass
            now = datetime.now(timezone.utc)
            agents = await self.store.list_agents()
            for agent in agents:
                if not agent.last_seen:
                    if agent.online:
                        await self.store.set_agent_online(agent.device_id or agent.agent_id, False)
                    continue
                if now - agent.last_seen <= timedelta(
                    seconds=self._offline_after_s
                ):
                    continue

                active = await self.list_active_tasks(agent.device_id or agent.agent_id)
                working = [t for t in active if t.status == TaskStatus.WORKING]
                if working:
                    # Agent still executing; keep online, do not requeue anything.
                    continue

                # Requeue every ASSIGNED (not yet started) task of this agent.
                for stale in active:
                    if stale.status == TaskStatus.ASSIGNED:
                        await self.store.update_task_status(
                            task_id=stale.task_id,
                            status=TaskStatus.QUEUED.value,
                            assigned_at=None,
                        )
                        await self.store.enqueue(stale.task_id, stale.agent_id)
                        logger.info(
                            "returned task %s to queue (agent offline)",
                            stale.task_id,
                        )
                # No task is active anymore (any WORKING would have kept us online
                # above); recompute keeps current_task_id consistent.
                await self._refresh_current_task(
                    agent.device_id or agent.agent_id
                )

                if agent.online:
                    await self.store.set_agent_online(agent.device_id or agent.agent_id, False)
                    logger.info("agent %s marked offline", agent.agent_id)
