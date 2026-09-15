from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from agent_mesh.edge.config_writer import (
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
    read_permission,
    read_system_prompt,
)
from agent_mesh.edge.execution import Executor
from agent_mesh.edge.rest_client import EdgeRestClient
from agent_mesh.edge.runner import TaskRunnerMixin
from agent_mesh.edge.upgrade import UpgradeMixin, cleanup_upgrade_tmp
from agent_mesh.shared.constants import VERSION
from agent_mesh.shared.schemas import Task

logger = logging.getLogger(__name__)


class EdgeAgent(TaskRunnerMixin, UpgradeMixin):
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
        permission: dict | None = None,
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
        permission = permission or read_permission(self.install_dir)
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
            permission=permission,
        )
        self._stop_event = asyncio.Event()
        self._last_cpu_sample = None
        self._running: dict[str, asyncio.Task] = {}
        self._max_concurrent = 2
        self._last_upgrade_attempt = 0.0
        self._upgrade_failures = 0
        self._upgrade_exhausted = False
        # Clear any upgrade temp files stranded by a previously interrupted run.
        cleanup_upgrade_tmp(self.install_dir)

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

                # Self-upgrade only when completely idle (no tasks executing and
                # none handed out this round), honouring the failure backoff.
                await self._maybe_upgrade(resp.get("upgrade"), bool(task_data_list))

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
        if "permission" in config:
            self.executor.permission = config.get("permission")
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
