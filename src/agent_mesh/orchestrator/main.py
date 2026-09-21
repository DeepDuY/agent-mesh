import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent_mesh.orchestrator.api import create_query_router
from agent_mesh.orchestrator.artifact_store import ArtifactStore
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import SERVER_VERSION

logger = logging.getLogger(__name__)


def create_app(
    config: OrchestratorConfig | None = None,
    store: TaskStore | None = None,
    artifact_store: ArtifactStore | None = None,
    start_sweepers: bool = True,
) -> tuple[FastAPI, TaskStore, ArtifactStore]:
    config = config or OrchestratorConfig()

    if store is None:
        store, store_backend = _build_store(config)
        init_backend = True
    else:
        store_backend = store.store
        init_backend = False

    artifact_store = artifact_store or ArtifactStore(
        base_dir=config.artifact_dir,
        max_size_mb=config.artifact_max_size_mb,
        max_total_mb=config.artifact_max_total_mb,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        logger.info("starting orchestrator")
        if init_backend:
            await store_backend.initialize()
        if start_sweepers:
            await store.start_sweepers()
        try:
            yield
        finally:
            if start_sweepers:
                await store.stop_sweepers()
            if init_backend:
                await store_backend.close()

    app = FastAPI(
        title="agent-mesh-orchestrator",
        version=SERVER_VERSION,
        redirect_slashes=False,
        lifespan=lifespan,
    )

    query_router = create_query_router(config, store, artifact_store)
    app.include_router(query_router)
    app.state.store = store

    static_dir = Path(__file__).with_name("web_ui")
    static_app = StaticFiles(directory=str(static_dir))

    async def _static_no_cache(scope, receive, send):
        """Wrap static files with Cache-Control: no-cache so browsers revalidate
        (the Web UI JS/CSS changes frequently; heuristic caching serves stale
        versions otherwise)."""
        if scope["type"] != "http":
            await static_app(scope, receive, send)
            return
        default_send = send

        async def patched_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await default_send(message)

        await static_app(scope, receive, patched_send)

    app.mount("/static", _static_no_cache, name="static")

    @app.get("/")
    async def _root():
        return FileResponse(str(static_dir / "index.html"))

    return app, store, artifact_store


def _build_store(config: OrchestratorConfig) -> tuple[TaskStore, Any]:
    if config.db_type == "pg":
        from agent_mesh.orchestrator.store.pg import PostgresStore

        store_backend = PostgresStore(
            dsn=config.pg_dsn,
            host=config.pg_host,
            port=config.pg_port,
            user=config.pg_user,
            password=config.pg_password,
            database=config.pg_database,
            min_size=config.pg_min_connections,
            max_size=config.pg_max_connections,
        )
    else:
        from agent_mesh.orchestrator.store.sqlite import SQLiteStore

        store_backend = SQLiteStore(config.db_path)

    store = TaskStore(
        store=store_backend,
        sweep_interval_s=config.sweep_interval_s,
        offline_after_s=config.offline_after_s,
    )
    return store, store_backend


def _resolve_workers(config: OrchestratorConfig) -> int:
    """Return the effective worker count. Only single-process is supported.

    ``AGENT_MESH_WORKERS>1`` is accepted for forward compatibility but does not
    start multiple processes: the in-process realtime hub and the non-atomic
    heartbeat capacity check are unsafe under multiple workers (see
    docs/known-issues.md §1/§2). A warning is logged and 1 is returned.
    """
    if config.workers > 1:
        logger.warning(
            "AGENT_MESH_WORKERS=%d is ignored: only single-process is supported, "
            "running 1 worker (see docs/known-issues.md)",
            config.workers,
        )
    return 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = OrchestratorConfig()
    _resolve_workers(config)
    if not config.token:
        logger.warning(
            "AGENT_MESH_TOKEN is not set: the global edge token is disabled. "
            "Set it (or use per-user/per-agent tokens) before exposing the edge API."
        )
    store, store_backend = _build_store(config)

    async def _init():
        await store_backend.initialize()

    asyncio.run(_init())

    _run_single_process(config, store)


def _run_single_process(config, store):
    """Single-process mode."""
    import uvicorn

    app, _, _ = create_app(config, store)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
