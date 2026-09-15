from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from agent_mesh.edge.execution import resolve_workdir
from agent_mesh.shared.schemas import ArtifactRef, Task

logger = logging.getLogger(__name__)


class TaskRunnerMixin:
    """Claimed-task lifecycle: attachments, execution, artifact upload, cancel."""

    async def _download_attachments(self, task: Task, workdir: Path) -> None:
        """Fetch task attachments into the workdir before execution.

        Downloads happen before the executor's pre-snapshot, so attached files
        are never collected back as new artifacts. The downloaded bytes are
        verified against the md5 recorded at upload time.
        """
        if not task.attachments:
            return
        for att in task.attachments:
            dest = workdir / att.filename
            md5 = await self.client.download_file(att.download_url, str(dest))
            if att.md5 and md5 != att.md5:
                raise RuntimeError(
                    f"md5 mismatch for {att.filename} "
                    f"(expected {att.md5}, got {md5})"
                )
            logger.info("downloaded attachment %s for task %s", dest, task.task_id)

    async def _execute(self, task: Task) -> None:
        logger.info("executing task %s", task.task_id)
        cancel_event = asyncio.Event()
        monitor = asyncio.create_task(self._monitor_cancel(task.task_id, cancel_event))
        try:
            # Tell orchestrator we started, so it does not mark us offline.
            try:
                await self.client.mark_started(task.task_id)
            except Exception as e:
                logger.warning("mark_started failed: %s", e)

            # Fetch attached files into the workdir before running so the task
            # can use them by filename. On failure, mark the task failed with an
            # explicit reason instead of running without the files.
            workdir = resolve_workdir(task, self.workdir)
            try:
                await self._download_attachments(task, workdir)
            except Exception as e:
                if cancel_event.is_set():
                    return
                logger.warning(
                    "attachment download failed for task %s: %s", task.task_id, e
                )
                await self.client.submit_result(
                    task_id=task.task_id,
                    agent_id=self.agent_id,
                    status="failed",
                    exit_code=-3,
                    stdout_tail="",
                    stderr_tail=f"attachment download failed: {e}",
                    duration_ms=0,
                    summary="attachment download failed",
                )
                return

            async def _report_logs(entries: list[dict[str, str]]) -> None:
                try:
                    await self.client.post_task_log(task.task_id, entries)
                except Exception as e:
                    logger.debug("task log upload failed for %s: %s", task.task_id, e)

            # A cancel may have landed while we were preparing (e.g. within the
            # assigned -> working window). Re-check so we never start executing
            # a task the orchestrator has already cancelled.
            if not cancel_event.is_set():
                try:
                    st = await self.client.get_task_status(task.task_id)
                    if (st.get("task") or {}).get("status") == "cancelled":
                        cancel_event.set()
                        logger.info(
                            "task %s already cancelled; not starting execution",
                            task.task_id,
                        )
                except Exception as e:
                    logger.debug(
                        "pre-exec status check failed for %s: %s", task.task_id, e
                    )
            if cancel_event.is_set():
                return

            outcome = await self.executor.run_task(
                task, cancel_event, log_callback=_report_logs
            )

            if cancel_event.is_set():
                # Task was cancelled by the orchestrator; do not submit a result
                # (the task is already in a terminal CANCELLED state).
                logger.info("task %s cancelled, skipping result submission", task.task_id)
                return

            # Collect artifacts from workdir.
            artifact_files: list[tuple[str, bytes]] = []
            for f in outcome.artifacts_paths:
                p = workdir / f
                if p.exists() and p.is_file():
                    try:
                        artifact_files.append((p.name, p.read_bytes()))
                    except Exception as e:
                        logger.warning("cannot read artifact %s: %s", p, e)
            if artifact_files:
                try:
                    upload_resp = await self.client.upload_artifacts(
                        task.task_id, artifact_files
                    )
                    refs = upload_resp.get("artifacts", [])
                    outcome.artifacts = [
                        ArtifactRef(**r) for r in refs
                    ]
                except Exception as e:
                    logger.warning("artifact upload failed: %s", e)

            result = self.executor.build_task_result(outcome)
            await self.client.submit_result(
                task_id=task.task_id,
                agent_id=self.agent_id,
                mode=result.mode,
                status=result.status,
                exit_code=result.exit_code,
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
                artifacts=[a.model_dump() for a in result.artifacts],
                duration_ms=result.duration_ms,
                summary=result.summary,
                session_id=result.session_id,
            )
            logger.info(
                "submitted result for task %s status=%s exit=%s",
                task.task_id,
                result.status,
                result.exit_code,
            )
        except Exception as e:
            logger.exception("execute failed: %s", e)
            if not cancel_event.is_set():
                await self.client.submit_result(
                    task_id=task.task_id,
                    agent_id=self.agent_id,
                    status="failed",
                    exit_code=-2,
                    stdout_tail="",
                    stderr_tail=str(e),
                    duration_ms=0,
                    summary="edge execution exception",
                )
        finally:
            monitor.cancel()
            try:
                await monitor
            except (asyncio.CancelledError, Exception):
                pass

    async def _monitor_cancel(self, task_id: str, cancel_event: asyncio.Event) -> None:
        """Poll the orchestrator for the task's status; signal cancellation."""
        while not self._stop_event.is_set():
            try:
                resp = await self.client.get_task_status(task_id)
                task_data = resp.get("task")
                if task_data and task_data.get("status") == "cancelled":
                    logger.info("task %s cancelled by orchestrator, terminating execution", task_id)
                    cancel_event.set()
                    return
            except Exception as e:
                logger.debug("cancel monitor error for %s: %s", task_id, e)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()), timeout=self.heartbeat_s
                )
            except asyncio.TimeoutError:
                pass
