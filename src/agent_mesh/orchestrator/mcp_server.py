from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from mcp.server import MCPServer

from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import VERSION, TaskStatus
from agent_mesh.shared.schemas import Constraints, FileRef, TaskResult, extract_telemetry

logger = logging.getLogger(__name__)


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def create_mcp_server(config: OrchestratorConfig, store: TaskStore) -> MCPServer:
    """Build the MCP tool server exposing agent-mesh orchestration to the main agent.

    All tools return plain JSON-serialisable dicts. Agents are identified by their
    numeric `id`, their `device_id` (machine-id from /etc/machine-id or a persisted
    random id), or their display `agent_id`; use the numeric id or device_id when
    possible.
    """
    server = MCPServer(
        name="agent-mesh-orchestrator",
        version=VERSION,
    )

    @server.tool(
        title="poll_for_task",
        description=(
            "Edge node long-poll/heartbeat. An installed edge agent calls this "
            "periodically to register itself (identified by `device_id`, the "
            "machine-id) and receive the next task(s) assigned to it. `agent_id` is a "
            "free-form display name reported by the node; `device_id` is the stable "
            "device key. `running_tasks` is an optional list of task ids this node is "
            "currently executing (used for multi-task re-dispatch recovery). Returns "
            "a `tasks` array of tasks to run (plus a backwards-compatible single "
            "`task` view) and `server_ts`. Normal callers are edge agents, not the "
            "control plane."
        ),
    )
    async def poll_for_task(
        agent_id: str,
        device_id: str,
        runtime: str | None = None,
        hostname: str | None = None,
        os: str | None = None,
        distro: str | None = None,
        arch: str | None = None,
        version: str | None = None,
        cpu_percent: float | None = None,
        mem_percent: float | None = None,
        mem_used_mb: float | None = None,
        mem_total_mb: float | None = None,
        running_tasks: list[str] | None = None,
    ) -> dict[str, Any]:
        logger.debug("poll_for_task agent=%s device_id=%s runtime=%s", agent_id, device_id, runtime)
        # The explicit tool params double as the wire schema; the telemetry
        # subset is whitelist-extracted from the registry (see shared.schemas).
        telemetry = extract_telemetry({
            "os": os,
            "distro": distro,
            "arch": arch,
            "cpu_percent": cpu_percent,
            "mem_percent": mem_percent,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
        })
        tasks, key = await store.heartbeat(
            agent_id, device_id, runtime, hostname, version, telemetry,
            running_tasks=running_tasks or [],
        )
        resp: dict[str, Any] = {
            "tasks": [t.model_dump_json_safe() for t in tasks],
            "task": tasks[0].model_dump_json_safe() if tasks else None,
            "server_ts": _now_ts(),
        }
        # LLM config sync channel (global defaults + per-agent overrides).
        config_version = await store.store.get_setting("config_version")
        resp["config_version"] = config_version or "0"
        agent = await store.resolve_agent(device_id or agent_id)
        if agent is not None:
            from agent_mesh.orchestrator.api.edge import _resolve_edge_config

            resp["config"] = await _resolve_edge_config(store, agent)
        return resp

    @server.tool(
        title="submit_result",
        description=(
            "Report the outcome of a task back to the orchestrator. `status` must be "
            "'completed' or 'failed'; `exit_code` is the process exit code; "
            "`stdout_tail`/`stderr_tail` are truncated output tails; `artifacts` is an "
            "optional list of artifact descriptors; `summary` is a short human summary. "
            "Normal callers are edge agents."
        ),
    )
    async def submit_result(
        task_id: str,
        agent_id: str,
        status: str,
        exit_code: int,
        stdout_tail: str = "",
        stderr_tail: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        duration_ms: int = 0,
        summary: str = "",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        logger.info(
            "submit_result task=%s agent=%s status=%s exit=%s",
            task_id,
            agent_id,
            status,
            exit_code,
        )
        if status not in ("completed", "failed"):
            return {"accepted": False, "error": "status must be 'completed' or 'failed'"}
        result = TaskResult(
            status=status,  # type: ignore[arg-type]
            exit_code=exit_code,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            artifacts=artifacts or [],
            duration_ms=duration_ms,
            summary=summary,
            session_id=session_id,
        )
        accepted = await store.submit_result(task_id, result)
        return {"accepted": accepted}

    @server.tool(
        title="get_task_status",
        description=(
            "Look up a single task by its `task_id` (e.g. 't-1a2b3c4d'). Returns "
            "{'found': false} if the task does not exist, otherwise the full task "
            "object including status ('queued'/'assigned'/'working'/'completed'/"
            "'failed'/'timed_out') and its result."
        ),
    )
    async def get_task_status(task_id: str) -> dict[str, Any]:
        task = await store.get_task(task_id)
        if task is None:
            return {"found": False}
        return {"found": True, "task": task.model_dump_json_safe()}

    @server.tool(
        title="list_tasks",
        description=(
            "List tasks, most recent first. Optional filters: `agent_id` (numeric id, "
            "MAC, or display name), `status` (one of queued/assigned/working/completed/"
            "failed/timed_out/cancelled), and `limit` (default 100). Returns {'tasks': [...]}."
        ),
    )
    async def list_tasks(
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        try:
            status_enum = TaskStatus(status) if status else None
        except ValueError:
            return {"error": "invalid status", "accepted": False}
        tasks = await store.list_tasks(
            agent_id=agent_id,
            status=status_enum,
            limit=limit,
        )
        return {"tasks": [t.model_dump_json_safe() for t in tasks]}

    @server.tool(
        title="cancel_task",
        description=(
            "Cancel a task by its `task_id` (e.g. 't-1a2b3c4d'). Queued tasks are "
            "removed from the queue; assigned/working tasks are marked 'cancelled' "
            "and the edge agent will terminate the running process on its next poll. "
            "Returns {'accepted': true} on success, or {'accepted': false, 'error': ...} "
            "if the task does not exist or is already in a terminal state."
        ),
    )
    async def cancel_task(task_id: str) -> dict[str, Any]:
        task = await store.get_task(task_id)
        if task is None:
            return {"found": False, "accepted": False}
        accepted = await store.cancel_task(task_id)
        if not accepted:
            return {
                "accepted": False,
                "error": "task is already in a terminal state",
            }
        logger.info("MCP cancelled task %s", task_id)
        return {"accepted": True, "task_id": task_id, "status": TaskStatus.CANCELLED.value}

    @server.tool(
        title="dispatch_task",
        description=(
            "Dispatch a task to an agent. `agent_id` accepts the agent's numeric id, "
            "its device_id (machine-id), or its display name. `instruction` is the instruction to "
            "run. `mode` selects execution style: 'command' runs `instruction` verbatim "
            "in a shell (no LLM), 'llm' (default) hands it to the agent's LLM runtime. "
            "Optional: `timeout_s` (default 300), `workdir`, `max_retries`, "
            "`depends_on` (list of task ids that must finish first), "
            "`model`, `output_limit` (default 200000), `session_id` (opencode session id "
            "to continue from a previous llm task; read it from a prior task's "
            "`result.session_id`), `skills` (list of skill names the agent may consult; "
            "the edge injects the skill-library instructions into the prompt), "
            "`attachments` (list of file-library ids, `f-...`; upload files first via "
            "`POST /api/files` then reference them here — the edge downloads them into "
            "the task workdir and md5-verifies before running). Returns "
            "the created task_id and 'queued' status. Raises an error if the target agent "
            "is not registered."
        ),
    )
    async def dispatch_task(
        agent_id: str,
        instruction: str,
        mode: str = "llm",
        timeout_s: int = 300,
        workdir: str = ".",
        max_retries: int = 0,
        depends_on: list[str] | None = None,
        model: str | None = None,
        output_limit: int = 200_000,
        session_id: str | None = None,
        skills: list[str] | None = None,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        logger.info(
            "dispatch_task agent=%s mode=%s instruction_len=%s", agent_id, mode, len(instruction)
        )
        if mode not in ("command", "llm"):
            return {"accepted": False, "error": "mode must be 'command' or 'llm'"}
        model_error = await store.validate_llm_model(agent_id, mode, model)
        if model_error:
            return {"accepted": False, "error": model_error}
        if mode == "command":
            denial = await store.check_command_permission(agent_id, instruction)
            if denial:
                await store.store.append_task_event(
                    task_id="",
                    event_type="permission_denied",
                    agent_id=agent_id,
                    details={
                        "mode": "command",
                        "instruction": instruction,
                        "reason": denial,
                        "source": "mcp",
                    },
                )
                return {"accepted": False, "error": denial}
        constraints = Constraints(
            workdir=workdir,
            timeout_s=timeout_s,
            model=model,
            output_limit=output_limit,
            session_id=session_id,
            skills=skills,
        )
        refs: list[FileRef] = []
        if attachments:
            rows = await store.store.get_files_by_ids(attachments)
            found = {r["file_id"]: r for r in rows}
            for fid in attachments:
                if fid not in found:
                    return {"accepted": False, "error": f"attachment file not found: {fid}"}
                r = found[fid]
                refs.append(
                    FileRef(
                        file_id=r["file_id"],
                        filename=r["filename"],
                        size=r["size"],
                        content_type=r.get("content_type") or "application/octet-stream",
                        md5=r["md5"],
                        download_url=f"/api/files/{r['file_id']}",
                    )
                )
        try:
            task_id = await store.dispatch(
                agent_id=agent_id,
                instruction=instruction,
                mode=mode,
                constraints=constraints,
                max_retries=max_retries,
                depends_on=depends_on,
                attachments=refs,
            )
        except ValueError as e:
            return {"accepted": False, "error": str(e)}
        return {"task_id": task_id, "status": TaskStatus.QUEUED.value}

    @server.tool(
        title="list_agents",
        description=(
            "List all registered agents (nodes). Each entry includes numeric `id`, "
            "`device_id` (machine-id, the stable device key), `agent_id` (display "
            "name), `alias`, `description` (node's own note), "
            "`effective_description` (node note, or the bound template's description "
            "when the node has none — use this to pick a node), `template_name`, "
            "`hostname`, `os`, `runtime`, `online`, `last_seen` and "
            "`current_task_id`. Returns {'agents': [...]}."
        ),
    )
    async def list_agents() -> dict[str, Any]:
        agents = await store.list_agents()
        await store.describe_agents(agents)
        return {"agents": [a.model_dump_json_safe() for a in agents]}

    @server.tool(
        title="get_agent",
        description=(
            "Get a single agent by numeric `id`, `device_id` (machine-id), or display "
            "name. Returns {'found': false} if not found."
        ),
    )
    async def get_agent(agent_id: str) -> dict[str, Any]:
        agent = await store.resolve_agent(agent_id)
        if agent is None:
            return {"found": False}
        await store.describe_agents([agent])
        return {"found": True, "agent": agent.model_dump_json_safe()}

    @server.tool(
        title="get_agent_detail",
        description=(
            "Get detailed info about an agent (by numeric id, device_id, or display "
            "name): its profile plus the 20 most recent tasks. Returns {'found': "
            "false} if not found."
        ),
    )
    async def get_agent_detail(agent_id: str) -> dict[str, Any]:
        agent = await store.resolve_agent(agent_id)
        if agent is None:
            return {"found": False}
        await store.describe_agents([agent])
        key = agent.device_id or agent.agent_id
        tasks = await store.list_tasks(agent_id=key, limit=20)
        from agent_mesh.shared.schemas import AgentDetail
        detail = AgentDetail(
            **agent.model_dump(),
            tasks=[t.model_dump_json_safe() for t in tasks],
        )
        return {"found": True, "agent": detail.model_dump_json_safe()}

    @server.tool(
        title="set_agent_alias",
        description=(
            "Set or clear the human-friendly alias (备注名) for an agent, matched by "
            "numeric id, device_id (machine-id), or display name. Pass `alias` as null "
            "to clear it."
        ),
    )
    async def set_agent_alias(agent_id: str, alias: str | None = None) -> dict[str, Any]:
        agent = await store.resolve_agent(agent_id)
        if agent is None:
            return {"found": False}
        await store.store.set_agent_alias_by_id(agent.id, alias)
        agent.alias = alias
        return {"agent": agent.model_dump_json_safe()}

    @server.tool(
        title="get_task_logs",
        description=(
            "Fetch the live execution log stream of a task (text the LLM generated, "
            "errors, completion summary). Pass `after_id` to fetch only entries newer "
            "than the last one you saw (incremental polling). Returns {'logs': [...]} "
            "plus the `next_id` to pass back on the next call."
        ),
    )
    async def get_task_logs(task_id: str, after_id: int = 0) -> dict[str, Any]:
        logs = await store.store.list_task_logs(task_id, after_id=after_id, limit=500)
        return {"task_id": task_id, "logs": logs, "next_id": logs[-1]["id"] if logs else after_id}

    @server.tool(
        title="list_skills",
        description=(
            "List the skills available in the orchestrator's skill library. Each entry "
            "contains only the summary: `name`, `description`, `version`, `enabled`. "
            "Full skill content is never exposed here; a skill must be downloaded via "
            "`GET /api/skills/{name}/download` when needed. Use this to decide whether a "
            "task should instruct the edge agent to consult a skill."
        ),
    )
    async def list_skills() -> dict[str, Any]:
        skills = await store.store.list_skills()
        return {"skills": [s for s in skills if s.get("enabled")]}

    @server.tool(
        title="list_models",
        description=(
            "List the LLM model ids the operator has configured as available "
            "(`settings.llm_models`). Pass one of these as `model` when dispatching "
            "an llm task. An empty list means no allow-list is configured (any "
            "gateway-supported model id may be used). Returns {'models': [...]}."
        ),
    )
    async def list_models() -> dict[str, Any]:
        return {"models": await store.allowed_models()}

    return server
