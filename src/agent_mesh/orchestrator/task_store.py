from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_mesh.orchestrator.permissions import PermissionMixin
from agent_mesh.orchestrator.realtime import publish
from agent_mesh.orchestrator.scheduler import SchedulerMixin
from agent_mesh.orchestrator.store.base import AbstractStore, _new_run_id, _new_task_id
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


@dataclass
class GatedDispatch:
    """Result of :meth:`TaskStore.dispatch_gated`."""

    task_id: str
    status: str  # "queued" | "pending_review" | "denied"
    score: int | None = None
    reason: str | None = None
    reviewed: bool = False


class TaskStore(TenancyMixin, PermissionMixin, SweeperMixin, SchedulerMixin):
    """Task store + state machine + heartbeat registry backed by an AbstractStore.

    Split across mixins:

    * :class:`~agent_mesh.orchestrator.tenancy.TenancyMixin` -- ACL/task visibility
    * :class:`~agent_mesh.orchestrator.permissions.PermissionMixin` -- model/permission/config
    * :class:`~agent_mesh.orchestrator.sweeper.SweeperMixin` -- timeout/offline scans
    * :class:`~agent_mesh.orchestrator.scheduler.SchedulerMixin` -- cron schedules
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
        owner_team_ids: str | None = None,
        run_id: str | None = None,
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
            owner_team_ids=owner_team_ids,
            run_id=run_id,
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
        team_ids: list[str] | None = None,
        run_id: str | None = None,
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
            team_ids=list(team_ids or []),
            run_id=run_id,
        )
        await self.store.create_task(
            task, dispatched_by=dispatched_by, user_id=user_id, team_ids=list(team_ids or [])
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
        team_ids: list[str] | None = None,
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
            team_ids=list(team_ids or []),
            finished_at=now,
        )
        await self.store.create_task(
            task, dispatched_by=dispatched_by, user_id=user_id, team_ids=list(team_ids or [])
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

    async def dispatch_pending_review(
        self,
        *,
        agent_id: str,
        instruction: str,
        mode: str,
        score: int | None,
        reason: str | None,
        constraints: Constraints | None = None,
        attachments: list[FileRef] | None = None,
        depends_on: list[str] | None = None,
        max_retries: int = 0,
        dispatched_by: str | None = None,
        user_id: str | None = None,
        team_ids: list[str] | None = None,
        run_id: str | None = None,
    ) -> str:
        """Create a task held by the AI review gate.

        The task is **not** enqueued, so no edge node can claim it while it waits
        for a human approve/reject decision in the Web console. The full dispatch
        context (constraints/attachments/dependencies) is stored so approval can
        run it unchanged.
        """
        target = await self._resolve_agent(agent_id)
        queue_key = (target.device_id or target.agent_id) if target else agent_id
        task = Task(
            task_id=_new_task_id(),
            agent_id=queue_key,
            mode=mode,  # type: ignore[arg-type]
            instruction=instruction,
            constraints=constraints or Constraints(),
            status=TaskStatus.PENDING_REVIEW,
            max_retries=max_retries,
            attachments=list(attachments or []),
            depends_on=list(depends_on or []),
            user_id=user_id,
            team_ids=list(team_ids or []),
            run_id=run_id,
            review_score=score,
            review_reason=reason,
        )
        await self.store.create_task(
            task, dispatched_by=dispatched_by, user_id=user_id, team_ids=list(team_ids or [])
        )
        logger.info(
            "task %s held for review (score=%s) agent=%s", task.task_id, score, queue_key
        )
        publish("tasks_changed")
        return task.task_id

    async def record_review(
        self,
        *,
        task_id: str,
        agent_id: str | None,
        mode: str | None,
        score: int | None,
        threshold: int | None,
        action: str,
        reason: str | None,
        violations: list[Any] | None,
        model: str | None,
        source: str,
        actor: str | None,
    ) -> None:
        """Persist a review decision (audit row + denormalised task columns)."""
        await self.store.create_task_review(
            task_id=task_id,
            agent_id=agent_id,
            mode=mode,
            score=score,
            threshold=threshold,
            verdict="allow" if action == "allowed" else "block",
            action=action,
            reason=reason,
            violations=violations,
            model=model,
            source=source,
            actor=actor,
        )
        if score is not None or reason is not None:
            await self.store.set_task_review(task_id, score, reason)

    async def review_permission(self, agent: AgentStatus | None) -> dict[str, Any]:
        """Effective permission profile governing ``agent`` (template > global)."""
        template = (
            await self.store.get_template(agent.template_id)
            if agent is not None and agent.template_id
            else None
        )
        return await self.effective_permission(agent, template)

    async def review_outcome(
        self,
        *,
        agent: AgentStatus | None,
        agent_id: str,
        instruction: str,
        mode: str,
        user: dict[str, Any],
        team_ids: list[str] | None = None,
    ) -> Any:
        """Run the AI review gate for one task, or ``None`` when out of scope.

        Separated from dispatch so a batch can run all reviews concurrently
        (the reviewer round-trip is the slow part) before enqueueing.
        """
        from agent_mesh.orchestrator import review as review_mod

        policy = await review_mod.get_policy(self)
        if agent is None or not review_mod.scope_matches(
            policy, agent=agent, team_ids=team_ids, user=user
        ):
            return None
        # Fill display fields so the reviewer sees the node's description.
        await self.describe_agents([agent])
        permission = await self.review_permission(agent)
        return await review_mod.review_task(
            self,
            policy,
            instruction=instruction,
            mode=mode,
            agent=agent,
            permission=permission,
        )

    async def _finalize_gated(
        self,
        *,
        agent_id: str,
        instruction: str,
        mode: str,
        constraints: Constraints,
        outcome: Any,
        user: dict[str, Any],
        dispatched_by: str | None = None,
        max_retries: int = 0,
        depends_on: list[str] | None = None,
        attachments: list[FileRef] | None = None,
        team_ids: list[str] | None = None,
        run_id: str | None = None,
    ) -> GatedDispatch:
        """Apply a (possibly pre-computed) review outcome to a dispatch."""
        user_id = user.get("user_id")

        async def _dispatch() -> str:
            return await self.dispatch(
                agent_id=agent_id,
                instruction=instruction,
                mode=mode,
                constraints=constraints,
                max_retries=max_retries,
                depends_on=depends_on,
                dispatched_by=dispatched_by,
                attachments=attachments,
                user_id=user_id,
                team_ids=team_ids,
                run_id=run_id,
            )

        async def _audit(task_id: str, action: str) -> None:
            # Recording the audit trail must never break the dispatch itself:
            # the gate decision (allow/deny/pending) is already applied above.
            try:
                await self.record_review(
                    task_id=task_id,
                    agent_id=agent_id,
                    mode=mode,
                    score=outcome.score if outcome else None,
                    threshold=outcome.threshold if outcome else None,
                    action=action,
                    reason=outcome.reason if outcome else None,
                    violations=outcome.violations if outcome else None,
                    model=outcome.model if outcome else None,
                    source="dispatch",
                    actor="ai",
                )
                await self.store.append_task_event(
                    task_id=task_id,
                    event_type="ai_review",
                    agent_id=agent_id,
                    user_id=user_id,
                    details={
                        "action": action,
                        "score": outcome.score if outcome else None,
                        "threshold": outcome.threshold if outcome else None,
                        "reason": outcome.reason if outcome else None,
                        "violations": outcome.violations if outcome else [],
                        "model": outcome.model if outcome else None,
                    },
                )
            except Exception:
                logger.exception("failed to record AI review audit for task=%s", task_id)

        if outcome is None:
            return GatedDispatch(await _dispatch(), TaskStatus.QUEUED.value)

        if outcome.action == "allow":
            task_id = await _dispatch()
            await _audit(task_id, "allowed")
            return GatedDispatch(
                task_id, TaskStatus.QUEUED.value, outcome.score, outcome.reason, True
            )

        if outcome.action == "pending":
            task_id = await self.dispatch_pending_review(
                agent_id=agent_id,
                instruction=instruction,
                mode=mode,
                score=outcome.score,
                reason=outcome.reason,
                constraints=constraints,
                attachments=attachments,
                depends_on=depends_on,
                max_retries=max_retries,
                dispatched_by=dispatched_by,
                user_id=user_id,
                team_ids=team_ids,
                run_id=run_id,
            )
            await _audit(task_id, "pending_review")
            return GatedDispatch(
                task_id, TaskStatus.PENDING_REVIEW.value, outcome.score, outcome.reason, True
            )

        # outcome.action == "deny"
        task_id = await self.dispatch_denied(
            agent_id=agent_id,
            instruction=instruction,
            mode=mode,
            reason=outcome.reason,
            dispatched_by=dispatched_by,
            user_id=user_id,
            team_ids=team_ids,
        )
        await _audit(task_id, "denied")
        logger.warning(
            "dispatch denied by AI review task=%s agent=%s user=%s score=%s reason=%s",
            task_id, agent_id, user_id, outcome.score, outcome.reason,
        )
        return GatedDispatch(
            task_id, TaskStatus.DENIED.value, outcome.score, outcome.reason, True
        )

    async def dispatch_gated(
        self,
        *,
        agent_id: str,
        instruction: str,
        mode: str,
        constraints: Constraints,
        user: dict[str, Any],
        dispatched_by: str | None = None,
        max_retries: int = 0,
        depends_on: list[str] | None = None,
        attachments: list[FileRef] | None = None,
        team_ids: list[str] | None = None,
        run_id: str | None = None,
    ) -> GatedDispatch:
        """Dispatch a task, first passing it through the optional AI review gate.

        Returns a :class:`GatedDispatch` describing whether it was queued,
        held as ``pending_review``, or denied. The gate is skipped entirely when
        the review policy is disabled or the task is out of scope.
        """
        target = await self._resolve_agent(agent_id)
        outcome = await self.review_outcome(
            agent=target,
            agent_id=agent_id,
            instruction=instruction,
            mode=mode,
            user=user,
            team_ids=team_ids,
        )
        return await self._finalize_gated(
            agent_id=agent_id,
            instruction=instruction,
            mode=mode,
            constraints=constraints,
            outcome=outcome,
            user=user,
            dispatched_by=dispatched_by,
            max_retries=max_retries,
            depends_on=depends_on,
            attachments=attachments,
            team_ids=team_ids,
            run_id=run_id,
        )

    async def approve_reviewed_task(self, task_id: str, *, actor: str | None) -> bool:
        """Approve a held task: move it to the queue so a node can claim it."""
        task = await self.get_task(task_id)
        if task is None or task.status != TaskStatus.PENDING_REVIEW:
            return False
        ok = await self.store.update_task_status(
            task_id=task_id,
            status=TaskStatus.QUEUED.value,
            expected_status=TaskStatus.PENDING_REVIEW.value,
        )
        if not ok:
            return False
        await self.store.enqueue(task_id, task.agent_id)
        await self.record_review(
            task_id=task_id,
            agent_id=task.agent_id,
            mode=task.mode,
            score=task.review_score,
            threshold=None,
            action="allowed",
            reason=task.review_reason,
            violations=None,
            model=None,
            source="approve",
            actor=actor,
        )
        await self.store.append_task_event(
            task_id=task_id,
            event_type="review_approved",
            details={"actor": actor, "previous_score": task.review_score},
        )
        logger.info("task %s approved by %s", task_id, actor)
        publish("tasks_changed")
        return True

    async def reject_reviewed_task(
        self, task_id: str, *, actor: str | None, reason: str | None = None
    ) -> bool:
        """Reject a held task: mark it denied with the reviewer's reason."""
        task = await self.get_task(task_id)
        if task is None or task.status != TaskStatus.PENDING_REVIEW:
            return False
        now = datetime.now(timezone.utc)
        ok = await self.store.update_task_status(
            task_id=task_id,
            status=TaskStatus.DENIED.value,
            finished_at=now,
            expected_status=TaskStatus.PENDING_REVIEW.value,
        )
        if not ok:
            return False
        final_reason = reason or task.review_reason or "人工审核拒绝"
        await self.store.set_task_result(
            task_id,
            TaskResult(
                status="denied",
                mode=task.mode,  # type: ignore[arg-type]
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=0,
                summary=final_reason,
            ),
        )
        await self.record_review(
            task_id=task_id,
            agent_id=task.agent_id,
            mode=task.mode,
            score=task.review_score,
            threshold=None,
            action="denied",
            reason=final_reason,
            violations=None,
            model=None,
            source="reject",
            actor=actor,
        )
        await self.store.append_task_event(
            task_id=task_id,
            event_type="review_rejected",
            details={"actor": actor, "reason": final_reason},
        )
        logger.info("task %s rejected by %s: %s", task_id, actor, final_reason)
        publish("tasks_changed")
        return True

    async def expire_reviewed_task(self, task_id: str) -> bool:
        """Auto-deny a `pending_review` task that no one handled in time."""
        task = await self.get_task(task_id)
        if task is None or task.status != TaskStatus.PENDING_REVIEW:
            return False
        reason = "审核超时未处理，已自动拦截"
        now = datetime.now(timezone.utc)
        ok = await self.store.update_task_status(
            task_id=task_id,
            status=TaskStatus.DENIED.value,
            finished_at=now,
            expected_status=TaskStatus.PENDING_REVIEW.value,
        )
        if not ok:
            return False
        await self.store.set_task_result(
            task_id,
            TaskResult(
                status="denied",
                mode=task.mode,  # type: ignore[arg-type]
                exit_code=0,
                stdout_tail="",
                stderr_tail="",
                duration_ms=0,
                summary=reason,
            ),
        )
        await self.record_review(
            task_id=task_id,
            agent_id=task.agent_id,
            mode=task.mode,
            score=task.review_score,
            threshold=None,
            action="denied",
            reason=reason,
            violations=None,
            model=None,
            source="expire",
            actor="system",
        )
        await self.store.append_task_event(
            task_id=task_id, event_type="review_expired", details={"reason": reason}
        )
        logger.info("task %s review expired -> denied", task_id)
        publish("tasks_changed")
        return True

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

    # ------------------------------------------------------------------
    # Runs (batch dispatch grouping)
    # ------------------------------------------------------------------
    @staticmethod
    def new_run_id() -> str:
        return _new_run_id()

    async def list_run_task_ids(
        self,
        run_id: str,
        owner_user_id: str | None = None,
        owner_team_ids: str | None = None,
    ) -> list[str]:
        """All task ids belonging to a run, in creation order (oldest first)."""
        tasks = await self.store.list_tasks(
            run_id=run_id,
            limit=100000,
            offset=0,
            owner_user_id=owner_user_id,
            owner_team_ids=owner_team_ids,
        )
        return [t.task_id for t in tasks]

    async def run_status_counts(
        self,
        run_id: str,
        owner_user_id: str | None = None,
        owner_team_ids: str | None = None,
    ) -> dict[str, int]:
        return await self.store.count_tasks_by_status(
            run_id, owner_user_id=owner_user_id, owner_team_ids=owner_team_ids
        )

    async def cancel_run(
        self,
        run_id: str,
        owner_user_id: str | None = None,
        owner_team_ids: str | None = None,
    ) -> dict[str, int]:
        """Cancel every non-terminal task in a run. Returns counts."""
        tasks = await self.store.list_tasks(
            run_id=run_id,
            limit=100000,
            owner_user_id=owner_user_id,
            owner_team_ids=owner_team_ids,
        )
        cancelled = skipped = 0
        for t in tasks:
            if await self.cancel_task(t.task_id):
                cancelled += 1
            else:
                skipped += 1
        return {"total": len(tasks), "cancelled": cancelled, "skipped": skipped}

    async def retry_run(
        self,
        run_id: str,
        owner_user_id: str | None = None,
        owner_team_ids: str | None = None,
    ) -> dict[str, int]:
        """Re-queue failed/timed-out tasks in a run. Returns counts."""
        retryable = {TaskStatus.FAILED, TaskStatus.TIMED_OUT}
        tasks = await self.store.list_tasks(
            run_id=run_id,
            limit=100000,
            owner_user_id=owner_user_id,
            owner_team_ids=owner_team_ids,
        )
        retried = skipped = 0
        for t in tasks:
            if t.status not in retryable:
                skipped += 1
                continue
            ok = await self.store.update_task_status(
                task_id=t.task_id,
                status=TaskStatus.QUEUED.value,
                retry_count=t.retry_count + 1,
                expected_status=t.status.value,
            )
            if not ok:
                skipped += 1
                continue
            await self.store.enqueue(t.task_id, t.agent_id)
            retried += 1
        if retried:
            publish("tasks_changed")
        return {"total": len(tasks), "retried": retried, "skipped": skipped}
