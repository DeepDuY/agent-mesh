import asyncio
from pathlib import Path

from agent_mesh.edge.execution.command import run_command
from agent_mesh.edge.execution.common import ExecutionOutcome
from agent_mesh.edge.execution.llm import run_llm
from agent_mesh.shared.schemas import Task, TaskResult

__all__ = ["Executor", "ExecutionOutcome", "resolve_workdir"]


def resolve_workdir(task: Task, default_workdir: str = ".") -> Path:
    """Effective workdir for a task.

    Prefers the task's explicit ``workdir`` (``Constraints.workdir``). When the
    dispatcher did not specify one (empty or the default ``.``), falls back to a
    per-task subdirectory under the edge's configured default
    (``<EDGE_WORKDIR>/tasks/<task_id>``) so concurrently running tasks never
    share a directory (their opencode.json, attachments and artifacts would
    otherwise collide). Explicit workdirs are used verbatim and the caller is
    responsible for isolation.
    """
    raw = (task.constraints.workdir or "").strip()
    if raw and raw != ".":
        path = Path(raw).expanduser().resolve()
    else:
        base = Path(default_workdir or ".").expanduser().resolve()
        path = base / "tasks" / task.task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


class Executor:
    def __init__(
        self,
        runtime: str,
        llm_api_key: str,
        llm_base_url: str,
        llm_model: str,
        edge_token: str = "",
        orchestrator_base_url: str = "",
        default_workdir: str = ".",
        system_prompt: str = "",
        llm_models: list[str] | None = None,
        permission: dict | None = None,
    ):
        self.runtime = runtime
        self.llm_api_key = llm_api_key
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.edge_token = edge_token
        self.orchestrator_base_url = orchestrator_base_url
        self.default_workdir = default_workdir or "."
        self.system_prompt = system_prompt or ""
        self.llm_models = list(llm_models or [])
        self.permission = permission

    async def run_task(
        self, task: Task, cancel_event: asyncio.Event | None = None,
        log_callback=None,
    ) -> ExecutionOutcome:
        workdir = resolve_workdir(task, self.default_workdir)

        if task.mode == "command":
            return await run_command(
                task, workdir, cancel_event, log_callback,
                permission=self.permission,
            )
        return await run_llm(
            task, workdir, self.runtime, self.llm_api_key, self.llm_base_url,
            self.llm_model, cancel_event, self.edge_token, self.orchestrator_base_url,
            log_callback,
            system_prompt=self.system_prompt,
            llm_models=self.llm_models,
            permission=self.permission,
        )

    def build_task_result(self, outcome: ExecutionOutcome) -> TaskResult:
        return TaskResult(
            status="completed" if outcome.exit_code == 0 else "failed",
            mode=outcome.mode,
            exit_code=outcome.exit_code,
            stdout_tail=outcome.stdout_tail,
            stderr_tail=outcome.stderr_tail,
            artifacts=outcome.artifacts,
            duration_ms=outcome.duration_ms,
            summary=outcome.summary,
            session_id=outcome.session_id,
        )
