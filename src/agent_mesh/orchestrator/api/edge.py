from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import TaskResult, extract_telemetry

logger = logging.getLogger(__name__)


async def _max_concurrent(store: TaskStore) -> int:
    value = await store.store.get_setting("max_concurrent")
    try:
        return max(1, int(value or 2))
    except ValueError:
        return 2



def _ver_parts(value: str) -> list[int]:
    return [int(x) for x in re.split(r"[.\-_]", str(value)) if x.isdigit()] or [0]


def _ver_ge(a: str, b: str) -> bool:
    return _ver_parts(a) >= _ver_parts(b)


def _bootstrap_version(config: OrchestratorConfig) -> str | None:
    version_path = Path(config.db_path).parent / "bootstrap" / "VERSION"
    try:
        value = version_path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _bootstrap_package_exists(config: OrchestratorConfig, filename: str) -> bool:
    pkg_dir = Path(config.db_path).parent / "bootstrap"
    return (pkg_dir / filename).exists()


async def _auto_upgrade_enabled(store: TaskStore) -> bool:
    """Whether stale nodes should auto-upgrade. Setting `auto_upgrade`, default on."""
    value = await store.store.get_setting("auto_upgrade")
    return (value or "1").strip().lower() not in ("0", "false", "no", "")


async def _resolve_edge_config(store: TaskStore, agent) -> dict[str, str]:
    """Resolve the effective edge config.

    Precedence (v1):
      * model:    node `llm_model` > bound template `llm_model` > global default
      * prompt:   node `system_prompt` + bound template `system_prompt`
                  (concatenated; the built-in base prompt is added by the edge)
      * api key / base url: node override > global

    No model default is hardcoded: when nothing is configured `llm_model` is an
    empty string and llm dispatch is rejected until an operator configures one.
    """
    llm_api_key = (await store.store.get_setting("llm_api_key")) or ""
    llm_base_url = (await store.store.get_setting("llm_base_url")) or ""
    llm_model = (await store.store.get_setting("llm_model")) or ""
    llm_models = (await store.store.get_setting("llm_models")) or ""

    template = None
    if agent is not None and agent.template_id:
        template = await store.store.get_template(agent.template_id)

    if agent is not None:
        if agent.llm_api_key:
            llm_api_key = agent.llm_api_key
        if agent.llm_base_url:
            llm_base_url = agent.llm_base_url
        if agent.llm_model:
            llm_model = agent.llm_model
    if (not agent or not agent.llm_model) and template is not None and template.get("llm_model"):
        llm_model = template["llm_model"]

    node_sp = (agent.system_prompt if agent is not None else "") or ""
    tpl_sp = (template.get("system_prompt") if template is not None else "") or ""
    system_prompt = "\n\n".join(
        p.strip() for p in (node_sp, tpl_sp) if p and p.strip()
    )

    # Permission precedence: bound template > global default > built-in readonly.
    # Permission is a template-level capability (no node/task override).
    permission = await store.effective_permission(agent, template)

    return {
        "llm_api_key": llm_api_key,
        "llm_base_url": llm_base_url,
        "llm_model": llm_model,
        "llm_models": llm_models,
        "system_prompt": system_prompt,
        "permission": permission,
    }


def mount_edge_routes(
    router: APIRouter,
    store: TaskStore,
    config: OrchestratorConfig,
    require_any_token,
) -> None:

    @router.post("/edge/poll_for_task")
    async def edge_poll_for_task(
        request: Request,
        auth: dict[str, Any] = Depends(require_any_token),
    ) -> dict[str, Any]:
        body = await request.json()
        agent_id = body.get("agent_id", "")
        device_id = body.get("device_id") or body.get("mac")
        runtime = body.get("runtime")
        hostname = body.get("hostname")
        version = body.get("version")
        # Telemetry (system + metrics) is whitelist-extracted from the registry;
        # unknown fields are ignored, missing fields default to None.
        telemetry = extract_telemetry(body)

        # An agent's own token is bound to its stable key: it may only poll for
        # its own node, never impersonate another device.
        if auth.get("auth") == "agent":
            expected = auth.get("device_id")
            if device_id and expected and device_id != expected:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="agent token is bound to a different device",
                )

        task, key = await store.heartbeat(
            agent_id, device_id, runtime, hostname, version, telemetry,
            running_tasks=body.get("running_tasks") or [],
        )
        agent = await store.resolve_agent(key)

        tasks = [t.model_dump_json_safe() for t in task]
        resp: dict[str, Any] = {
            "tasks": tasks,
            # Backwards-compatible single-task view (legacy edges read this).
            "task": tasks[0] if tasks else None,
            "server_ts": _now_ts(),
            "max_concurrent": await _max_concurrent(store),
        }

        # --- Independent per-agent token (issued exactly once on registration) ---
        # The edge persists it into edge.env and authenticates with it from then
        # on, so the agent no longer depends on a user login session.
        if agent is not None:
            agent_token = await store.ensure_agent_token(agent.id)
            if agent_token:
                resp["agent_token"] = agent_token
            # Record which user operates this machine (many-to-many; used by
            # the later multi-user management).
            if auth.get("auth") == "user":
                await store.store.add_agent_user(agent.id, auth["user_id"])

        # --- LLM config sync (config_version + resolved config) ---
        config_version = await store.store.get_setting("config_version")
        resp["config_version"] = config_version or "0"
        resp["config"] = await _resolve_edge_config(store, agent)

        # --- Agent self-upgrade directive ---
        if agent is not None:
            current_ver = _bootstrap_version(config)
            target_ver: str | None = None
            if agent.upgrade_requested and agent.upgrade_version:
                # Manual per-node upgrade request.
                target_ver = agent.upgrade_version
                if agent.version and _ver_ge(agent.version, agent.upgrade_version):
                    # The node already reports the target version; upgrade done.
                    await store.store.clear_agent_upgrade(agent.id)
                    target_ver = None
            elif current_ver and agent.version and not _ver_ge(agent.version, current_ver):
                # Auto-upgrade: the node reports an older version than the
                # published bootstrap package (no manual request needed).
                if await _auto_upgrade_enabled(store):
                    logger.info(
                        "auto-upgrade agent=%s from %s to %s",
                        key, agent.version, current_ver,
                    )
                    target_ver = current_ver
            if target_ver and current_ver:
                os_name = agent.os or "linux"
                arch_name = agent.arch or "x64"
                filename = f"agent-mesh-agent-{os_name}-{arch_name}.tar.gz"
                if _bootstrap_package_exists(config, filename):
                    resp["upgrade"] = {
                        "version": target_ver,
                        "filename": filename,
                    }
                else:
                    logger.warning(
                        "upgrade requested for agent %s but package %s is missing",
                        key, filename,
                    )
        return resp

    @router.post("/edge/submit_result")
    async def edge_submit_result(
        request: Request,
        _auth: None = Depends(require_any_token),
    ) -> dict[str, Any]:
        body = await request.json()
        status = body.get("status")
        if status not in ("completed", "failed"):
            return {"accepted": False, "error": "invalid status"}
        artifacts = body.get("artifacts", [])
        for a in artifacts:
            await store.store.save_artifact(
                task_id=body.get("task_id", ""),
                artifact_id=a.get("artifact_id"),
                filename=a.get("filename"),
                size=a.get("size"),
                content_type=a.get("content_type"),
                storage_path=a.get("download_url", ""),
            )
        result = TaskResult(
            status=status,
            mode=body.get("mode", "llm"),
            exit_code=body.get("exit_code", -1),
            stdout_tail=body.get("stdout_tail", ""),
            stderr_tail=body.get("stderr_tail", ""),
            artifacts=artifacts,
            duration_ms=body.get("duration_ms", 0),
            summary=body.get("summary", ""),
            session_id=body.get("session_id"),
        )
        accepted = await store.submit_result(body.get("task_id", ""), result)
        # Audit an edge-side permission denial (command matcher rejected it).
        combined = f"{result.summary}\n{result.stderr_tail}"
        if status == "failed" and "权限被拒绝" in combined:
            await store.store.append_task_event(
                task_id=body.get("task_id", ""),
                event_type="permission_denied",
                agent_id=body.get("agent_id"),
                details={
                    "mode": body.get("mode", "command"),
                    "summary": result.summary,
                    "source": "edge",
                },
            )
        return {"accepted": accepted}

    @router.post("/edge/get_task_status")
    async def edge_get_task_status(
        request: Request,
        _auth: None = Depends(require_any_token),
    ) -> dict[str, Any]:
        body = await request.json()
        task = await store.get_task(body.get("task_id", ""))
        if task is None:
            return {"found": False}
        return {"found": True, "task": task.model_dump_json_safe()}

    @router.post("/edge/task_log")
    async def edge_task_log(
        request: Request,
        _auth: None = Depends(require_any_token),
    ) -> dict[str, Any]:
        """Receive a batch of live execution log entries from an edge agent.

        Body: ``{"task_id": "...", "entries": [{"kind": "text|error|complete|raw", "content": "..."}]}``.
        Entries are appended in order; the Web UI polls ``GET /api/tasks/{id}/logs``
        with ``?after_id=`` to consume the stream incrementally.
        """
        body = await request.json()
        task_id = body.get("task_id", "")
        entries = body.get("entries") or []
        if not task_id or not entries:
            return {"accepted": False, "error": "task_id and entries required"}
        valid_kinds = {"text", "error", "complete", "raw"}
        clean = [
            {
                "kind": e.get("kind", "raw") if e.get("kind") in valid_kinds else "raw",
                "content": str(e.get("content", "")),
            }
            for e in entries
            if isinstance(e, dict)
        ]
        if not clean:
            return {"accepted": False, "error": "no valid entries"}
        await store.store.append_task_logs(task_id, clean)
        return {"accepted": True, "count": len(clean)}

    @router.post("/edge/mark_started")
    async def edge_mark_started(
        request: Request,
        _auth: None = Depends(require_any_token),
    ) -> dict[str, Any]:
        body = await request.json()
        accepted = await store.mark_started(body.get("task_id", ""))
        return {"accepted": accepted}


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())
