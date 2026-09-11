import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.streamable_http import TransportSecuritySettings

from agent_mesh.orchestrator.api import create_query_router
from agent_mesh.orchestrator.artifact_store import ArtifactStore
from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.mcp_server import create_mcp_server
from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)


def create_app(
    config: OrchestratorConfig | None = None,
    store: TaskStore | None = None,
    artifact_store: ArtifactStore | None = None,
    start_sweepers: bool = True,
) -> tuple[FastAPI, TaskStore, ArtifactStore]:
    config = config or OrchestratorConfig()

    if store is None:
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
        version="1.0.0",
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


async def _run_sse_server(
    mcp_server: Any,
    host: str,
    port: int,
    token: str,
    store: TaskStore,
) -> None:
    """Run the MCP SSE transport behind a Bearer-token auth middleware.

    Only user tokens (long-lived API tokens or short-lived session tokens) are
    accepted; the global ``AGENT_MESH_TOKEN`` is not valid on the MCP channel.
    """
    from starlette.responses import JSONResponse

    from agent_mesh.orchestrator.auth import resolve_token_user

    logger.info("starting MCP SSE server on %s:%s", host, port)
    app = mcp_server.sse_app(
        sse_path="/",
        message_path="/messages/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
        host=host,
    )

    async def _authorized(scope: dict, receive: Any, send: Any) -> bool:
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        user = await resolve_token_user(store, auth[7:])
        return user is not None

    async def _auth_wrapper(scope, receive, send) -> None:
        if scope["type"] == "http" and not await _authorized(scope, receive, send):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            _auth_wrapper,
            host=host,
            port=port,
            log_level="info",
        )
    )
    await server.serve()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = OrchestratorConfig()

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

    async def _init():
        await store_backend.initialize()

    asyncio.run(_init())

    mcp_server = create_mcp_server(config, store)

    if config.workers > 1:
        _run_multi_process(config, store, store_backend, mcp_server)
    else:
        _run_single_process(config, store, store_backend, mcp_server)


def _run_multi_process(config, store, store_backend, mcp_server):
    """Multi-worker mode: main process handles sweeper + MCP SSE, workers handle FastAPI HTTP."""
    import uvicorn

    async def _main_loop():
        await store.start_sweepers()

        sse_task = asyncio.create_task(
            _run_sse_server(mcp_server, config.host, config.port + 1, config.token, store)
        )

        app, _, artifact_store = create_app(
            config, store, start_sweepers=False
        )

        server_config = uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            workers=config.workers,
            log_level="info",
        )
        server = uvicorn.Server(server_config)
        http_task = asyncio.create_task(server.serve())

        try:
            await asyncio.gather(http_task, sse_task)
        finally:
            await store.stop_sweepers()
            await store_backend.close()

    asyncio.run(_main_loop())


def _run_single_process(config, store, store_backend, mcp_server):
    """Single-process mode: backward-compatible with original behavior."""
    import uvicorn

    app, _, artifact_store = create_app(config, store)

    async def _lifespan_wrapper():
        async with app.router.lifespan_context(app):
            sse_task = asyncio.create_task(
                _run_sse_server(
                    mcp_server,
                    host=config.host,
                    port=config.port + 1,
                    token=config.token,
                    store=store,
                )
            )
            server_config = uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                log_level="info",
            )
            server = uvicorn.Server(server_config)
            main_task = asyncio.create_task(server.serve())
            await asyncio.gather(main_task, sse_task)

    asyncio.run(_lifespan_wrapper())


if __name__ == "__main__":
    main()
