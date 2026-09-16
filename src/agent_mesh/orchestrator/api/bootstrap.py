from __future__ import annotations

import logging
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from agent_mesh.orchestrator.config import OrchestratorConfig, bootstrap_version
from agent_mesh.orchestrator.limits import LIMIT_SPECS, validate_limit
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.constants import SERVER_VERSION

logger = logging.getLogger(__name__)

_PKG_NAME_RE = re.compile(r"^agent-mesh-agent-(linux|darwin)-(x64|arm64)$")
_MAX_PACKAGE_BYTES = 500 * 1024 * 1024


def mount_bootstrap_routes(
    router: APIRouter,
    config: OrchestratorConfig,
    store: TaskStore,
    require_user_token,
    require_any_token,
    require_admin=None,
) -> None:
    # Global settings (LLM credentials, default permission, model allow-list)
    # are admin-only: they are a privilege-escalation surface for a compromised
    # agent token. Fall back to the user guard if none was supplied.
    require_admin = require_admin or require_user_token

    @router.get("/settings")
    async def get_settings(
        user: dict[str, Any] = Depends(require_admin),
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
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        body = await request.json()
        for key, value in body.items():
            if key in LIMIT_SPECS:
                error = validate_limit(key, value)
                if error:
                    raise HTTPException(status_code=400, detail=error)
                value = str(int(value))
            await store.store.set_setting(key, str(value))
        # Any change to LLM defaults, the fallback permission, or the probe
        # transfer timeout must be pushed to edges via config sync.
        if any(
            k in body
            for k in (
                "llm_api_key",
                "llm_base_url",
                "llm_model",
                "llm_models",
                "default_permission",
                "artifact_timeout_s",
            )
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
        token_check = "${TOKEN:-}"
        install_dir = "${INSTALL_DIR:-/opt/agent-mesh-agent}"

        # LLM configuration is deliberately NOT embedded here. Global settings
        # are delivered to a node over the heartbeat config-sync after it
        # registers (auth-security.md §9), so no LLM credential ever appears in
        # this script or in the process list during installation.
        #
        # OS/arch are detected on the TARGET machine (not baked in server-side),
        # so the same command works on linux/macOS and x64/arm64.
        script = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            f'BASE_URL="{base_url}"\n'
            "\n"
            "OS=$(uname -s | tr '[:upper:]' '[:lower:]')\n"
            'case "$OS" in\n'
            "    darwin) OS=darwin ;;\n"
            "    *)      OS=linux ;;\n"
            "esac\n"
            "ARCH=$(uname -m)\n"
            'case "$ARCH" in\n'
            "    x86_64|amd64) ARCH=x64 ;;\n"
            "    aarch64|arm64) ARCH=arm64 ;;\n"
            "esac\n"
            'PKG="agent-mesh-agent-${OS}-${ARCH}.tar.gz"\n'
            'INSTALL_URL="${BASE_URL}/api/bootstrap/${PKG}"\n'
            "\n"
            "for c in curl tar; do\n"
            '    command -v "$c" >/dev/null 2>&1 || { echo "ERROR: $c is required but not installed." >&2; exit 1; }\n'
            "done\n"
            f'if [ -z "{token_check}" ]; then\n'
            '    echo "ERROR: TOKEN environment variable is required." >&2\n'
            '    echo "  Usage: TOKEN=<your user token> bash <(curl ... /api/bootstrap/install.sh)" >&2\n'
            "    exit 1\n"
            "fi\n"
            "\n"
            "TMPDIR=$(mktemp -d)\n"
            'trap "rm -rf $TMPDIR" EXIT\n'
            'echo "==> Downloading ${PKG} from ${INSTALL_URL}"\n'
            'if ! curl -fsSL -H "Authorization: Bearer $TOKEN" "$INSTALL_URL" -o "$TMPDIR/agent.tar.gz"; then\n'
            '    echo "ERROR: failed to download the probe package (${PKG})." >&2\n'
            '    echo "  - check TOKEN is valid and the server is reachable" >&2\n'
            '    echo "  - make sure a probe package for ${OS}/${ARCH} was built and published" >&2\n'
            "    exit 1\n"
            "fi\n"
            'tar -xzf "$TMPDIR/agent.tar.gz" -C "$TMPDIR"\n'
            'PKG_DIR="$TMPDIR/agent-mesh-agent-${OS}-${ARCH}"\n'
            'if [ ! -f "$PKG_DIR/install.sh" ]; then\n'
            '    echo "ERROR: unexpected package layout (no install.sh in $PKG_DIR)" >&2\n'
            "    exit 1\n"
            "fi\n"
            f'INSTALL_DIR="{install_dir}" ORCHESTRATOR_URL="$BASE_URL" TOKEN="$TOKEN" '
            f'EDGE_ALIAS="${{EDGE_ALIAS:-}}" '
            'bash "$PKG_DIR/install.sh"\n'
        )
        return PlainTextResponse(script, media_type="text/x-shellscript")

    @router.get("/bootstrap/info")
    async def bootstrap_info(
        _admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        """Server version + published probe package info (shown on the config page)."""
        bootstrap_dir = Path(config.db_path).parent / "bootstrap"
        packages: list[dict[str, Any]] = []
        if bootstrap_dir.is_dir():
            for p in sorted(bootstrap_dir.glob("agent-mesh-agent-*.tar.gz")):
                st = p.stat()
                packages.append(
                    {
                        "filename": p.name,
                        "size": st.st_size,
                        "updated_at": datetime.fromtimestamp(
                            st.st_mtime, timezone.utc
                        ).isoformat(),
                    }
                )
        return {
            "server_version": SERVER_VERSION,
            "probe_version": bootstrap_version(config.db_path),
            "packages": packages,
        }

    @router.post("/bootstrap")
    async def upload_bootstrap(
        file: UploadFile = File(...),
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        """Publish/replace a probe package (.tar.gz) uploaded from the Web console.

        The archive is validated, its embedded ``VERSION`` is read, and it is
        placed under ``<db_dir>/bootstrap/`` so both new installs and node
        self-upgrades pick it up (see auth-security.md §8).
        """
        bootstrap_dir = Path(config.db_path).parent / "bootstrap"
        bootstrap_dir.mkdir(parents=True, exist_ok=True)

        tmp = bootstrap_dir / f".upload-{os.getpid()}.tar.gz"
        size = 0
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _MAX_PACKAGE_BYTES:
                        raise HTTPException(
                            status_code=413, detail="probe package too large (max 500MB)"
                        )
                    out.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="empty upload")

            try:
                tf = tarfile.open(tmp, "r:gz")
            except tarfile.TarError:
                raise HTTPException(
                    status_code=400, detail="not a valid .tar.gz probe package"
                )
            with tf:
                roots = {
                    m.name.split("/", 1)[0] for m in tf.getmembers() if m.name
                }
                roots.discard("")
                if len(roots) != 1:
                    raise HTTPException(
                        status_code=400,
                        detail="unexpected package layout (expected one top-level directory)",
                    )
                root = roots.pop()
                if not _PKG_NAME_RE.match(root):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"unexpected package name '{root}' "
                            "(expected agent-mesh-agent-<os>-<arch>)"
                        ),
                    )
                version = None
                install_sh = None
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    if m.name == f"{root}/VERSION":
                        version = (
                            (tf.extractfile(m).read() or b"")
                            .decode("utf-8", "replace")
                            .strip()
                        )
                    elif m.name == f"{root}/install.sh":
                        install_sh = tf.extractfile(m).read()
                if not version:
                    raise HTTPException(
                        status_code=400, detail="probe package is missing VERSION"
                    )

            target = bootstrap_dir / f"{root}.tar.gz"
            os.replace(tmp, target)
            tmp = None  # type: ignore[assignment]  # moved; nothing to clean up
            (bootstrap_dir / "VERSION").write_text(version + "\n", encoding="utf-8")
            if install_sh:
                (bootstrap_dir / "install.sh").write_bytes(install_sh)
        finally:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

        logger.info(
            "published probe package %s (version %s, %d bytes) by %s",
            target.name,
            version,
            size,
            admin["username"],
        )
        return {
            "uploaded": True,
            "filename": target.name,
            "probe_version": version,
            "size": size,
        }

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
