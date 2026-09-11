from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse

from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore


def mount_bootstrap_routes(
    router: APIRouter,
    config: OrchestratorConfig,
    store: TaskStore,
    require_user_token,
    require_any_token,
) -> None:

    @router.get("/settings")
    async def get_settings(
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        settings = await store.store.list_settings()
        # Expose the global token to admins only, so the web UI can generate
        # bootstrap install commands with a long-lived credential. The login
        # session token expires (default 24h) and must not be embedded in the
        # install command (the installed edge would break after expiry).
        if user.get("role") == "admin":
            settings["agent_mesh_token"] = config.token
        return {"settings": settings}

    @router.patch("/settings")
    async def patch_settings(
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        body = await request.json()
        for key, value in body.items():
            await store.store.set_setting(key, str(value))
        # Any change to LLM defaults must be pushed to edges via config sync.
        if any(
            k in body
            for k in ("llm_api_key", "llm_base_url", "llm_model", "llm_models")
        ):
            await store.bump_config_version()
        return {"settings": await store.store.list_settings()}

    @router.get("/bootstrap/install.sh")
    async def bootstrap_install_script(
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> PlainTextResponse:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "127.0.0.1:8000")
        scheme = request.headers.get("x-forwarded-proto") or "http"
        base_url = config.public_url or f"{scheme}://{host}"
        os_name = request.query_params.get("os", "linux")
        arch = request.query_params.get("arch", "x64")
        pkg = f"agent-mesh-agent-{os_name}-{arch}.tar.gz"
        token_check = "${TOKEN:-}"
        install_dir = "${INSTALL_DIR:-/opt/agent-mesh-agent}"

        # LLM configuration is deliberately NOT embedded here. Global settings
        # are delivered to a node over the heartbeat config-sync after it
        # registers (auth-security.md §9), so no LLM credential ever appears in
        # this script or in the process list during installation.
        script = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            f'INSTALL_URL="{base_url}/api/bootstrap/{pkg}"\n'
            "\n"
            "OS=$(uname -s | tr '[:upper:]' '[:lower:]')\n"
            "ARCH=$(uname -m)\n"
            'case "$ARCH" in\n'
            '    x86_64) ARCH="x64" ;;\n'
            '    aarch64|arm64) ARCH="arm64" ;;\n'
            "esac\n"
            "\n"
            f'ORCHESTRATOR_URL="{base_url}"\n'
            f'if [ -z "{token_check}" ]; then\n'
            '    echo "ERROR: TOKEN environment variable is required" >&2\n'
            "    exit 1\n"
            "fi\n"
            "\n"
            "TMPDIR=$(mktemp -d)\n"
            'trap "rm -rf $TMPDIR" EXIT\n'
            'curl -fsSL -H "Authorization: Bearer $TOKEN" "$INSTALL_URL" -o "$TMPDIR/agent.tar.gz"\n'
            'tar -xzf "$TMPDIR/agent.tar.gz" -C "$TMPDIR"\n'
            f'INSTALL_DIR="{install_dir}" ORCHESTRATOR_URL="$ORCHESTRATOR_URL" TOKEN="$TOKEN" '
            f'EDGE_ALIAS="${{EDGE_ALIAS:-}}" '
            f'bash "$TMPDIR/agent-mesh-agent-{os_name}-{arch}/install.sh"\n'
        )
        return PlainTextResponse(script, media_type="text/x-shellscript")

    @router.get("/bootstrap/{filename}")
    async def bootstrap_download(
        filename: str,
        _auth: None = Depends(require_any_token),
    ) -> FileResponse:
        """Download a bootstrap package.

        Uses ``require_any_token`` so an edge agent authenticated with the global
        token can self-download the upgrade package during agent self-upgrade.
        """
        bootstrap_dir = Path(config.db_path).parent / "bootstrap"
        file_path = bootstrap_dir / filename
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="bootstrap package not found")
        return FileResponse(file_path, filename=filename)
