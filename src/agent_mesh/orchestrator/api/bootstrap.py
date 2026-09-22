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

_PKG_NAME_RE = re.compile(r"^agent-mesh-agent-(linux|darwin|win32)-(x64|arm64)$")
_MAX_PACKAGE_BYTES = 500 * 1024 * 1024

# These values are interpolated into shell/PowerShell scripts that the user
# pipes into `bash` / `iex`, so they must never contain script metacharacters.
# A stray `Host: x"; rm -rf / #` would otherwise execute on the install target.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$")
_URL_RE = re.compile(r"^https?://[^\s\"'`$\\]+$")
_BOOTSTRAP_ROOT_FILES = frozenset({"install.sh", "install.ps1", "VERSION"})


def _request_base_url(request: Request, config: OrchestratorConfig) -> str:
    """Public base URL for generated installers, with a strict Host fallback.

    ``config.public_url`` wins; otherwise the request Host is validated so it
    cannot inject code into the returned script. Raises 400 when the host is
    unusable, rather than emitting a dangerous script.
    """
    if config.public_url:
        return config.public_url
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if not _HOST_RE.match(host):
        raise HTTPException(
            status_code=400,
            detail="invalid Host header; configure public_url or use a valid host:port",
        )
    scheme = request.headers.get("x-forwarded-proto") or "http"
    if scheme not in ("http", "https"):
        scheme = "http"
    return f"{scheme}://{host}"


def _safe_bootstrap_base(value: str | None) -> str:
    """Return an http(s) mirror URL, or "" when unset/invalid.

    ``bootstrap_download_base`` is embedded in the installer scripts; reject
    anything that is not a plain http(s) URL.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if not _URL_RE.match(value):
        logger.warning("ignoring invalid bootstrap_download_base setting: %r", value)
        return ""
    return value


def _valid_bootstrap_filename(filename: str) -> bool:
    if filename in _BOOTSTRAP_ROOT_FILES:
        return True
    if filename.endswith(".tar.gz"):
        return bool(_PKG_NAME_RE.match(filename[: -len(".tar.gz")]))
    return False


# Settings the bundled Web UI is allowed to read/return. Anything else (notably
# `session_secret`) stays server-side.
_PUBLIC_SETTINGS_KEYS = frozenset(
    {
        "public_url",
        "auto_upgrade",
        "max_concurrent",
        "task_log_poll_interval",
        "config_version",
        "default_permission",
        "llm_api_key",
        "llm_base_url",
        "llm_model",
        "llm_models",
        "bootstrap_download_base",
        "probe_release_repo",
        "probe_release_token",
        *LIMIT_SPECS.keys(),
    }
)


def _public_settings(
    settings: dict[str, str], user: dict[str, Any], config: OrchestratorConfig
) -> dict[str, str]:
    out = {k: v for k, v in settings.items() if k in _PUBLIC_SETTINGS_KEYS}
    # The global token is exposed to admins so the Web UI can build bootstrap
    # install commands (session tokens expire and must not be embedded).
    if user.get("role") == "admin":
        out["agent_mesh_token"] = config.token
    return out

# Windows bootstrap installer: the PowerShell counterpart of install.sh. It
# detects the architecture, downloads the win32 package with the bearer token,
# extracts it (tar.exe, present on Windows 10 1803+) and runs the packaged
# installer. Served by /bootstrap/install.ps1 for `irm ... | iex`.
_PS_INSTALL_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$BaseUrl = "__BASE_URL__"
$BootstrapBase = "__BOOTSTRAP_BASE__"
$Token = $env:TOKEN
if (-not $Token) {
    Write-Error 'TOKEN environment variable is required. Usage: $env:TOKEN="<token>"; irm <server>/api/bootstrap/install.ps1 | iex'
    exit 1
}
$arch = switch ($env:PROCESSOR_ARCHITECTURE) { 'ARM64' { 'arm64' } 'AMD64' { 'x64' } default { 'x64' } }
$pkg = "agent-mesh-agent-win32-$arch.tar.gz"
if ($BootstrapBase) {
    $url = "$($BootstrapBase.TrimEnd('/'))/$pkg"
    $headers = @{}
} else {
    $url = "$BaseUrl/api/bootstrap/$pkg"
    $headers = @{ Authorization = "Bearer $Token" }
}
$tmp = Join-Path $env:TEMP ("agent-mesh-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    Write-Host "==> Downloading $pkg from $url"
    Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $url -OutFile (Join-Path $tmp 'agent.tar.gz')
    Write-Host '==> Extracting'
    tar.exe -xzf (Join-Path $tmp 'agent.tar.gz') -C $tmp
    $pkgDir = Join-Path $tmp "agent-mesh-agent-win32-$arch"
    if (-not (Test-Path (Join-Path $pkgDir 'install.ps1'))) {
        Write-Error "unexpected package layout (no install.ps1 in $pkgDir)"
        exit 1
    }
    # -Force makes re-running the same command an idempotent (re)install: without
    # it an existing install aborts before the Scheduled Task is registered, which
    # is how a node ends up installed but not auto-starting.
    & (Join-Path $pkgDir 'install.ps1') -OrchestratorUrl $BaseUrl -Token $Token -AgentId $env:EDGE_ALIAS -Force
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
""".strip()


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

    def _require_ui(request: Request) -> None:
        """Restrict settings endpoints to the bundled Web UI.

        External API/agent callers get 404 so the settings surface (which holds
        credentials) is not reachable with a stolen token.
        """
        if request.headers.get("x-agent-mesh-ui") != "1":
            raise HTTPException(status_code=404, detail="Not Found")

    @router.get("/settings")
    async def get_settings(
        request: Request,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        _require_ui(request)
        settings = await store.store.list_settings()
        return {"settings": _public_settings(settings, user, config)}

    @router.patch("/settings")
    async def patch_settings(
        request: Request,
        user: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        _require_ui(request)
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
        return {"settings": _public_settings(await store.store.list_settings(), user, config)}

    @router.get("/bootstrap/install.sh")
    async def bootstrap_install_script(
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> PlainTextResponse:
        base_url = _request_base_url(request, config)
        bootstrap_base = _safe_bootstrap_base(
            await store.store.get_setting("bootstrap_download_base")
        )
        token_check = "${TOKEN:-}"
        install_dir = "${INSTALL_DIR:-/opt/agent-mesh-agent}"
        # LLM configuration is deliberately NOT embedded here. Global settings
        # are delivered to a node over the heartbeat config-sync after it
        # registers (auth-security.md §9), so no LLM credential ever appears in
        # this script or in the process list during installation.
        #
        # OS/arch are detected on the TARGET machine (not baked in server-side),
        # so the same command works on linux/macOS and x64/arm64.
        #
        # `bootstrap_download_base` (optional) points package downloads at an
        # external mirror (e.g. a GitHub Release). Empty -> serve locally with
        # the bearer token; set -> anonymous download from the mirror.
        script = (
            "#!/usr/bin/env bash\n"
            "set -e\n"
            f'BASE_URL="{base_url}"\n'
            f'BOOTSTRAP_BASE="{bootstrap_base}"\n'
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
            'CURL_ARGS=(-fsSL)\n'
            'if [ -n "$BOOTSTRAP_BASE" ]; then\n'
            '    INSTALL_URL="${BOOTSTRAP_BASE%/}/${PKG}"\n'
            "else\n"
            '    INSTALL_URL="${BASE_URL}/api/bootstrap/${PKG}"\n'
            '    CURL_ARGS+=(-H "Authorization: Bearer $TOKEN")\n'
            "fi\n"
            'echo "==> Downloading ${PKG} from ${INSTALL_URL}"\n'
            'if ! curl "${CURL_ARGS[@]}" "$INSTALL_URL" -o "$TMPDIR/agent.tar.gz"; then\n'
            '    echo "ERROR: failed to download the probe package (${PKG})." >&2\n'
            '    echo "  - check TOKEN is valid and the server/mirror is reachable" >&2\n'
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

    @router.get("/bootstrap/install.ps1")
    async def bootstrap_install_ps1(
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> PlainTextResponse:
        """PowerShell bootstrap installer (Windows counterpart of install.sh).

        OS is fixed to win32 and the arch is detected on the target; the same
        URL therefore works for x64/arm64. Run it as::

            $env:TOKEN='<token>'; irm <server>/api/bootstrap/install.ps1 | iex
        """
        base_url = _request_base_url(request, config)
        bootstrap_base = _safe_bootstrap_base(
            await store.store.get_setting("bootstrap_download_base")
        )
        script = (
            _PS_INSTALL_TEMPLATE.replace("__BASE_URL__", base_url)
            .replace("__BOOTSTRAP_BASE__", bootstrap_base)
        )
        return PlainTextResponse(script, media_type="text/plain")

    @router.post("/bootstrap/sync")
    async def bootstrap_sync(
        admin: dict[str, Any] = Depends(require_admin),
    ) -> dict[str, Any]:
        """Pull the latest prebuilt probe packages from the configured GitHub Release.

        Uses ``probe_release_repo`` (``<owner>/<repo>``) and optional
        ``probe_release_token``. Assets are written to ``<db_dir>/bootstrap/`` so
        installs and upgrades are served locally right after.
        """
        from agent_mesh.orchestrator.probe_release import sync_release
        import httpx

        repo = (await store.store.get_setting("probe_release_repo")) or ""
        if not repo:
            raise HTTPException(status_code=400, detail="probe_release_repo is not configured")
        token = (await store.store.get_setting("probe_release_token")) or ""
        bootstrap_dir = Path(config.db_path).parent / "bootstrap"
        try:
            result = await sync_release(repo, token, bootstrap_dir)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"failed to sync from GitHub: {e}")
        logger.info(
            "synced probe release %s (%s): %s",
            repo, result.get("tag"), [s["filename"] for s in result.get("synced", [])],
        )
        return result

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
        if not _valid_bootstrap_filename(filename):
            raise HTTPException(status_code=404, detail="bootstrap package not found")
        bootstrap_dir = Path(config.db_path).parent / "bootstrap"
        file_path = bootstrap_dir / filename
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="bootstrap package not found")
        return FileResponse(file_path, filename=filename)
