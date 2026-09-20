from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from agent_mesh.orchestrator.permissions import PermissionMixin
from agent_mesh.orchestrator.realtime import publish
from agent_mesh.orchestrator.store.base import AbstractStore, _new_task_id
from agent_mesh.orchestrator.sweeper import SweeperMixin
from agent_mesh.orchestrator.tenancy import TenancyMixin
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


class TaskStore(TenancyMixin, PermissionMixin, SweeperMixin):
    """Task store + state machine + heartbeat registry backed by an AbstractStore.

    Split across mixins:

    * :class:`~agent_mesh.orchestrator.tenancy.TenancyMixin` -- ACL/task visibility
    * :class:`~agent_mesh.orchestrator.permissions.PermissionMixin` -- model/permission/config
    * :class:`~agent_mesh.orchestrator.sweeper.SweeperMixin` -- timeout/offline scans
    """

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

    async def resolve_agent(self, agent_ref: str) -> AgentStatus | None:
        return await self._resolve_agent(agent_ref)

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------
    # A task is eligible once every dependency reached COMPLETED. Any terminal
    # non-success state blocks it permanently (the dependent is cancelled).
    _DEP_FAILED = frozenset(
        {
            TaskStatus.FAILED,
            TaskStatus.TIMED_OUT,
            TaskStatus.CANCELLED,
            TaskStatus.DENIED,
        }
    )

    async def _validate_dependencies(self, depends_on: list[str]) -> list[str]:
        deps: list[str] = []
        for dep_id in depends_on:
            if dep_id in deps:
                continue
            if await self.get_task(dep_id) is None:
                raise ValueError(f"dependency task not found: {dep_id}")
            deps.append(dep_id)
        # Defensive cycle check. The API cannot normally create a cycle (a task
        # may only depend on tasks that already exist), but a hand-edited DB could.
        done: set[str] = set()
        for dep_id in deps:
            await self._check_dependency_cycle(dep_id, set(), done)
        return deps

    async def _check_dependency_cycle(
        self, node: str, path: set[str], done: set[str]
    ) -> None:
        if node in done:
            return
        if node in path:
            raise ValueError(f"dependency cycle detected at {node}")
        path.add(node)
        task = await self.get_task(node)
        for dep in (task.depends_on if task else []):
            await self._check_dependency_cycle(dep, path, done)
        path.discard(node)
        done.add(node)

    async def _dependency_state(self, task: Task) -> tuple[str, list[str]]:
        """Return ("ready"|"pending"|"failed", failed_dependency_ids)."""
        if not task.depends_on:
            return "ready", []
        failed: list[str] = []
        for dep_id in task.depends_on:
            dep = await self.get_task(dep_id)
            if dep is None or dep.status in self._DEP_FAILED:
                failed.append(dep_id)
            elif dep.status != TaskStatus.COMPLETED:
                return "pending", []
        return ("failed", failed) if failed else ("ready", [])

    async def _cancel_for_dependencies(self, task_id: str, failed_deps: list[str]) -> None:
        now = datetime.now(timezone.utc)
        await self.store.update_task_status(
            task_id=task_id,
            status=TaskStatus.CANCELLED.value,
            finished_at=now,
        )
        await self.store.append_task_event(
            task_id=task_id,
            event_type="dependency_failed",
            details={"dependencies": failed_deps},
        )
        logger.info(
            "cancelled task %s: dependency not satisfied (%s)", task_id, failed_deps
        )

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
        deps = await self._validate_dependencies(depends_on or [])

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
            depends_on=deps,
            user_id=user_id,
            team_id=team_id,
        )
        await self.store.create_task(
            task, dispatched_by=dispatched_by, user_id=user_id, team_id=team_id
        )
        await self.store.enqueue(task.task_id, queue_key)
        logger.info("dispatched task %s for agent=%s mode=%s", task.task_id, queue_key, mode)
        publish("tasks_changed")
        return task.task_id

    async def dispatch_denied(
        self,
        agent_id: str,
        instruction: str,
        mode: str,
        reason: str,
        dispatched_by: str | None = None,
        user_id: str | None = None,
        team_id: str | None = None,
    ) -> str:
        """Record a dispatch that was rejected by policy as a visible task.

        The command was never executed (no queue entry, no edge contact), but the
        attempt is stored as a terminal ``denied`` task so it shows up in the task
        list with the denial reason as its summary — otherwise a rejected dispatch
        would leave no trace for the user.
        """
        target = await self._resolve_agent(agent_id)
        queue_key = (target.device_id or target.agent_id) if target else agent_id
        now = datetime.now(timezone.utc)
        task = Task(
            task_id=_new_task_id(),
            agent_id=queue_key,
            mode=mode,  # type: ignore[arg-type]
            instruction=instruction,
            constraints=Constraints(),
            status=TaskStatus.DENIED,
            user_id=user_id,
            team_id=team_id,
            finished_at=now,
        )
        await self.store.create_task(
            task, dispatched_by=dispatched_by, user_id=user_id, team_id=team_id
        )
        await self.store.set_task_result(
            task.task_id,
            TaskResult(
                status="denied",
                mode=mode,  # type: ignore[arg-type]
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=0,
                summary=reason,
            ),
        )
        logger.info("recorded denied dispatch %s for agent=%s", task.task_id, queue_key)
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
        ip_address: str | None = None,
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
            ip_address=ip_address,
        )
        if agent is None or not agent.online:
            publish("agents_changed")

        active = await self.list_active_tasks(key)
        running = set(running_tasks or [])
        resume = [t for t in active if t.task_id not in running]

        # Claim new tasks up to the concurrency cap. Iterate the queue in FIFO
        # order but skip (leave queued) tasks whose dependencies are not done,
        # so a blocked task does not head-of-line-block ready ones behind it.
        max_concurrent = await self.max_concurrent()
        capacity = max(0, max_concurrent - len(active))
        new_tasks: list[Task] = []
        if capacity > 0:
            for task_id in await self.store.peek_queue(key, limit=200):
                if capacity <= 0:
                    break
                task = await self.store.get_task(task_id)
                if not task or task.status != TaskStatus.QUEUED:
                    await self.store.remove_from_queue(task_id)
                    continue
                state, failed_deps = await self._dependency_state(task)
                if state == "pending":
                    continue
                if state == "failed":
                    await self.store.remove_from_queue(task_id)
                    await self._cancel_for_dependencies(task_id, failed_deps)
                    continue
                if not await self.store.dequeue_task(task_id):
                    continue  # another poller claimed it first
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
        if new_tasks:
            publish("tasks_changed")

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
        publish("tasks_changed")
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
        publish("tasks_changed")
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
            TaskStatus.DENIED,
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
        publish("tasks_changed")
        return True
