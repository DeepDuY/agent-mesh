from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import tarfile

from pathlib import Path

from agent_mesh.edge.config_writer import (
    WRAPPER_SCRIPT,
    apply_llm_config,
    get_arch,
    get_device_id,
    get_distro,
    get_hostname,
    get_os,
    parse_model_list,
    persist_edge_token,
    read_agent_version,
    read_edge_token,
    read_llm_models,
    read_system_prompt,
)
from agent_mesh.edge.execution import Executor, resolve_workdir
from agent_mesh.edge.rest_client import EdgeRestClient
from agent_mesh.shared.constants import VERSION
from agent_mesh.shared.schemas import ArtifactRef, Task

logger = logging.getLogger(__name__)


class EdgeAgent:
    def __init__(
        self,
        agent_id: str,
        orchestrator_url: str,
        token: str,
        runtime: str,
        workdir: str,
        llm_api_key: str,
        llm_base_url: str,
        llm_model: str,
        heartbeat_s: float = 3.0,
        install_dir: str | None = None,
        llm_models: str = "",
        system_prompt: str = "",
    ):
        self.agent_id = agent_id
        self.runtime = runtime
        self.workdir = workdir
        self.heartbeat_s = heartbeat_s
        self.install_dir = install_dir or "/opt/agent-mesh-agent"
        self.version = read_agent_version(self.install_dir) or VERSION
        # If edge.env already carries this agent's own token (persisted after
        # first registration), prefer it over the bootstrap credential.
        if token in ("", "change-me-shared-secret"):
            token = read_edge_token(self.install_dir) or token
        # Prefer config persisted by a previous config-sync (survives restarts).
        system_prompt = system_prompt or read_system_prompt(self.install_dir) or ""
        llm_models = llm_models or read_llm_models(self.install_dir) or ""
        self.client = EdgeRestClient(
            orchestrator_url.replace("/mcp", ""), token
        )
        self.executor = Executor(
            runtime=runtime,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            edge_token=self.client.token,
            orchestrator_base_url=self.client.base_url,
            default_workdir=workdir,
            system_prompt=system_prompt,
            llm_models=parse_model_list(llm_models),
        )
        self._stop_event = asyncio.Event()
        self._last_cpu_sample = None
        self._running: dict[str, asyncio.Task] = {}
        self._max_concurrent = 2

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_stop)

        logger.info("edge agent %s starting (version=%s)", self.agent_id, self.version)
        try:
            await self._loop()
        finally:
            await self.client.close()
            logger.info("edge agent %s stopped", self.agent_id)

    def _request_stop(self) -> None:
        logger.info("stop signal received")
        self._stop_event.set()

    async def _loop(self) -> None:
        backoff = 1.0
        device_id = get_device_id(self.install_dir)
        while not self._stop_event.is_set():
            try:
                metrics = self._collect_metrics()
                resp = await self.client.poll_for_task(
                    agent_id=self.agent_id,
                    device_id=device_id,
                    runtime=self.runtime,
                    hostname=get_hostname(),
                    os=get_os(),
                    distro=get_distro(),
                    arch=get_arch(),
                    version=self.version,
                    cpu_percent=metrics["cpu_percent"],
                    mem_percent=metrics["mem_percent"],
                    mem_used_mb=metrics["mem_used_mb"],
                    mem_total_mb=metrics["mem_total_mb"],
                    running_tasks=list(self._running.keys()),
                )
                backoff = 1.0

                # Adopt the concurrency cap announced by the orchestrator.
                mc = resp.get("max_concurrent")
                if mc is not None:
                    try:
                        self._max_concurrent = max(1, int(mc))
                    except (TypeError, ValueError):
                        pass

                task_data_list = resp.get("tasks")
                if not task_data_list:
                    task_data = resp.get("task")
                    task_data_list = [task_data] if task_data else []

                # The orchestrator issued this agent its own independent token
                # (first registration): persist it and switch to it.
                agent_token = resp.get("agent_token")
                if agent_token:
                    try:
                        persist_edge_token(self.install_dir, agent_token)
                        self.client.set_token(agent_token)
                        self.executor.edge_token = agent_token
                        logger.info(
                            "received independent agent token; switched to agent auth"
                        )
                    except Exception as e:
                        logger.warning("failed to persist agent token: %s", e)

                # A successful poll confirms any pending upgrade is healthy.
                self._confirm_upgrade_healthy()

                # LLM config sync: apply when the orchestrator's version differs.
                cfg_ver = resp.get("config_version")
                cfg = resp.get("config")
                if cfg is not None:
                    try:
                        self._apply_llm_config(cfg_ver, cfg)
                    except Exception as e:
                        logger.warning("apply llm config failed: %s", e)

                # Self-upgrade only when completely idle (no tasks executing).
                upg = resp.get("upgrade")
                if upg and not self._running and not task_data_list:
                    try:
                        await self._perform_upgrade(upg)
                    except Exception as e:
                        logger.exception("upgrade failed: %s", e)

                # Spawn execution for newly claimed tasks (concurrent, up to cap).
                for td in task_data_list:
                    if self._stop_event.is_set():
                        break
                    task = Task(**td)
                    if task.task_id in self._running:
                        continue
                    if len(self._running) >= self._max_concurrent:
                        logger.warning(
                            "at max concurrency (%s); task %s deferred to next poll",
                            self._max_concurrent, task.task_id,
                        )
                        continue
                    runner = asyncio.create_task(
                        self._execute(task), name=f"task-{task.task_id}"
                    )
                    self._running[task.task_id] = runner
                    runner.add_done_callback(
                        lambda _t, tid=task.task_id: self._running.pop(tid, None)
                    )
            except Exception as e:
                logger.exception("heartbeat error: %s", e)
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2
                continue

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.heartbeat_s
                )
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # LLM config sync
    # ------------------------------------------------------------------
    def _apply_llm_config(self, config_version: str | None, config: dict) -> None:
        if config_version is None:
            return
        local_ver = self._local_config_version()
        if local_ver == str(config_version):
            return
        self.executor.llm_api_key = config.get("llm_api_key", self.executor.llm_api_key)
        self.executor.llm_base_url = config.get("llm_base_url", self.executor.llm_base_url)
        self.executor.llm_model = config.get("llm_model", self.executor.llm_model)
        if "system_prompt" in config:
            self.executor.system_prompt = (config.get("system_prompt") or "")
        if "llm_models" in config:
            self.executor.llm_models = parse_model_list(config.get("llm_models") or "")
        apply_llm_config(self.install_dir, config, str(config_version))
        logger.info(
            "applied LLM config v%s (model=%s base_url=%s)",
            config_version, self.executor.llm_model, self.executor.llm_base_url,
        )

    def _local_config_version(self) -> str:
        version_path = Path(self.install_dir) / "etc" / "config_version"
        try:
            return version_path.read_text(encoding="utf-8").strip() or "0"
        except OSError:
            return "0"

    # ------------------------------------------------------------------
    # Resource metrics (heartbeat)
    # ------------------------------------------------------------------
    def _collect_metrics(self) -> dict:
        """Best-effort CPU / memory usage; None when unavailable."""
        cpu = mem_percent = mem_used_mb = mem_total_mb = None
        try:
            import psutil

            cpu = round(psutil.cpu_percent(interval=None) or 0.0, 1)
            vm = psutil.virtual_memory()
            mem_percent = round(vm.percent, 1)
            mem_used_mb = round(vm.used / (1024 * 1024), 1)
            mem_total_mb = round(vm.total / (1024 * 1024), 1)
        except Exception:
            logger.debug("resource metrics unavailable", exc_info=True)
        return {
            "cpu_percent": cpu,
            "mem_percent": mem_percent,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
        }

    # ------------------------------------------------------------------
    # Agent self-upgrade
    # ------------------------------------------------------------------
    def _confirm_upgrade_healthy(self) -> None:
        """After a successful poll, clear the upgrade rollback markers/backups."""
        install = Path(self.install_dir)
        marker = install / "etc" / "upgrading"
        if not marker.exists():
            return
        try:
            marker.unlink()
            (install / "etc" / "upgrade-started").unlink(missing_ok=True)
            (install / "bin" / "agent-mesh-edge.bin.old").unlink(missing_ok=True)
            (install / "etc" / "agent_version.bak").unlink(missing_ok=True)
            logger.info("upgrade confirmed healthy; removed rollback backup")
        except OSError as e:
            logger.warning("could not clear upgrade marker: %s", e)

    async def _perform_upgrade(self, upgrade: dict) -> None:
        """Download the new package, atomically replace the binary and restart."""
        version = upgrade.get("version")
        if not version:
            logger.warning("upgrade skipped: no version in directive")
            return
        install = Path(self.install_dir)
        bin_dir = install / "bin"
        wrapper = bin_dir / "agent-mesh-edge"
        bin_file = bin_dir / "agent-mesh-edge.bin"
        if not wrapper.exists() or not bin_file.exists():
            logger.warning(
                "upgrade skipped: %s is not an installed layout", install
            )
            return
        if get_os() == "win32":
            logger.warning("upgrade not supported on win32 yet")
            return

        os_name = get_os()
        arch = get_arch()
        filename = upgrade.get("filename") or f"agent-mesh-agent-{os_name}-{arch}.tar.gz"
        staging = install / "etc" / "upgrade"
        staging.mkdir(parents=True, exist_ok=True)
        pkg_path = staging / filename
        try:
            url = f"{self.client.base_url}/api/bootstrap/{filename}"
            logger.info("downloading upgrade package %s -> %s", url, pkg_path)
            await self.client.download_to(url, str(pkg_path))

            extract_dir = staging / "pkg"
            shutil.rmtree(extract_dir, ignore_errors=True)
            with tarfile.open(pkg_path, "r:gz") as tar:
                tar.extractall(extract_dir, filter="data")
            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            if not roots:
                raise RuntimeError("upgrade package is empty")
            pkg_root = roots[0]

            new_bin = pkg_root / "bin" / "agent-mesh-edge"
            if not new_bin.exists():
                raise RuntimeError("upgrade package missing bin/agent-mesh-edge")

            # Atomic replace with a rollback copy (`.bin.old`). The running
            # process IS this binary, so it must be swapped via rename
            # (os.replace) rather than overwritten in place, which fails on
            # Linux with "text file busy".
            backup = bin_dir / "agent-mesh-edge.bin.old"
            shutil.copy2(bin_file, backup)
            new_stage = bin_dir / "agent-mesh-edge.bin.new"
            shutil.copy2(new_bin, new_stage)
            os.chmod(new_stage, 0o755)
            os.replace(new_stage, bin_file)

            new_opencode = pkg_root / "bin" / "opencode"
            if new_opencode.exists():
                opencode_stage = bin_dir / "opencode.new"
                shutil.copy2(new_opencode, opencode_stage)
                os.chmod(opencode_stage, 0o755)
                os.replace(opencode_stage, bin_dir / "opencode")

            # Rollback-aware wrapper (so a crashed new binary does not boot-loop).
            (bin_dir / "agent-mesh-edge").write_text(WRAPPER_SCRIPT, encoding="utf-8")
            os.chmod(wrapper, 0o755)

            # Version manifest + pending-rollback markers (rollback in wrapper).
            (install / "etc").mkdir(parents=True, exist_ok=True)
            version_path = install / "etc" / "agent_version"
            if version_path.exists():
                shutil.copy2(version_path, install / "etc" / "agent_version.bak")
            version_path.write_text(version, encoding="utf-8")
            (install / "etc" / "upgrading").touch()
            logger.info("upgrade staged (v%s); restarting", version)
        except Exception as e:
            logger.exception("upgrade failed: %s", e)
            return
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        # Restart into the new version. Prefer the service manager so the
        # running process is actually replaced by the new binary (in-place
        # os.execv has proven unreliable for PyInstaller onefile binaries).
        try:
            import subprocess

            os_name = get_os()
            if os_name == "linux":
                subprocess.run(
                    ["systemctl", "restart", "agent-mesh-edge"],
                    check=False,
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # systemctl restart will SIGTERM this process (it is the
                # service), so reaching here is unlikely.
            elif os_name == "darwin":
                subprocess.run(
                    ["launchctl", "kickstart", "-k", "system/com.agentmesh.edge"],
                    check=False,
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception as e:
            logger.warning("service restart failed (%s); falling back to execv", e)

        # Fallback for non-service installs (dev mode / plain process).
        try:
            os.execv(str(wrapper), [str(wrapper)])
        except OSError as e:
            logger.exception("restart failed: %s", e)

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------
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


def main() -> None:
    import logging as _logging

    from agent_mesh.orchestrator.config import EdgeConfig

    _logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = EdgeConfig()
    agent_id = cfg.agent_id or get_hostname() or get_device_id(cfg.install_dir)
    agent = EdgeAgent(
        agent_id=agent_id,
        orchestrator_url=cfg.orchestrator_url,
        token=cfg.token,
        runtime=cfg.runtime,
        workdir=cfg.workdir,
        llm_api_key=cfg.llm_api_key,
        llm_base_url=cfg.llm_base_url,
        llm_model=cfg.llm_model,
        heartbeat_s=cfg.heartbeat_s,
        install_dir=cfg.install_dir,
        llm_models=cfg.llm_models,
        system_prompt=cfg.system_prompt,
    )
    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
