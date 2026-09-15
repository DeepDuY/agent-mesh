"""Background sweepers for TaskStore: timeout detection and offline requeue.

Extracted from TaskStore. Requires the host to provide ``self.store``,
``self._stop_event``, ``self._sweep_interval_s`` and ``self._offline_after_s``
(all set by ``TaskStore.__init__``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from agent_mesh.shared.constants import TaskStatus
from agent_mesh.shared.schemas import TaskResult

logger = logging.getLogger(__name__)


class SweeperMixin:
    """Periodic working-timeout and agent-offline scans."""

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
