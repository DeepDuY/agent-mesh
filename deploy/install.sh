#!/usr/bin/env bash
# Deploy the agent-mesh orchestrator to /opt/agent-mesh with a systemd service,
# then build and publish the edge probe package (opencode bundled).
#
# The probe is built with the deployed virtualenv (which has PyInstaller via the
# `dev` extra) but from the current repo checkout, and the resulting package is
# written to <INSTALL_DIR>/data/bootstrap/ where the orchestrator serves it.
# opencode is bundled into the probe: it is auto-detected locally or downloaded
# from the official GitHub releases if missing.
#
# Usage:
#   ./deploy/install.sh                 # deploy server + build probe
#   ./deploy/install.sh --no-probe      # server only (no opencode needed here)
#   ./deploy/install.sh --opencode /path/to/opencode
#   OPENCODE_BIN=/path/to/opencode ./deploy/install.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agent-mesh}"
SERVICE_USER="${SERVICE_USER:-root}"
ORCH_PORT="${AGENT_MESH_PORT:-8000}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

BUILD_PROBE=1
OPENCODE_BIN="${OPENCODE_BIN:-}"

usage() {
    cat <<EOF
Usage: ./deploy/install.sh [--no-probe] [--opencode PATH]
  --no-probe        skip building/publishing the edge probe package
  --opencode PATH   opencode binary to bundle (else auto-detected, or downloaded from GitHub)
  --help            show this help
Env: OPENCODE_BIN (same as --opencode), INSTALL_DIR, SERVICE_USER, AGENT_MESH_PORT
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-probe) BUILD_PROBE=0 ;;
        --opencode) OPENCODE_BIN="${2:-}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

echo "==> Deploying agent-mesh to ${INSTALL_DIR} (source: ${REPO_DIR})"

# ------------------------------------------------------------------
# Pre-flight
# ------------------------------------------------------------------
command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync is required" >&2; exit 1; }
if [ "$(id -u)" -ne 0 ]; then
    echo "WARNING: not running as root; systemd registration will likely fail" >&2
fi

# ------------------------------------------------------------------
# Stop existing service
# ------------------------------------------------------------------
systemctl stop agent-mesh-orchestrator 2>/dev/null || true

# ------------------------------------------------------------------
# Directory layout
# ------------------------------------------------------------------
mkdir -p "${INSTALL_DIR}"/{bin,lib,etc,log,run,data/artifacts,data/bootstrap}

# ------------------------------------------------------------------
# Copy source (runtime only; never build artifacts / internal docs)
# ------------------------------------------------------------------
rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='.pytest_cache' \
    --exclude='build' \
    --exclude='dist' \
    --exclude='data' \
    --exclude='.env' \
    --exclude='etc' \
    --exclude='code-review-report.md' \
    --exclude='评审补充澄清.md' \
    --exclude='mcp-server.json' \
    "${REPO_DIR}/" "${INSTALL_DIR}/lib/agent-mesh/"

# ------------------------------------------------------------------
# Virtualenv + dependencies
# ------------------------------------------------------------------
PYBIN="$(command -v python3.12 || command -v python3)"
if [ ! -d "${INSTALL_DIR}/lib/venv" ]; then
    echo "==> Creating virtual environment (${PYBIN})"
    "${PYBIN}" -m venv "${INSTALL_DIR}/lib/venv"
fi

echo "==> Installing dependencies"
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -U pip
# PyInstaller (dev extra) is only needed when building the probe.
EXTRAS=""
[ "${BUILD_PROBE}" = "1" ] && EXTRAS="[dev]"
# asyncpg>=0.30 has only manylinux_2_28 wheels; on older glibc the source build
# fails. Pin <0.30 which ships a broadly-compatible wheel (see known-issues §18).
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -e "${INSTALL_DIR}/lib/agent-mesh${EXTRAS}" 'asyncpg<0.30'

# ------------------------------------------------------------------
# Orchestrator env (idempotent: never clobber an existing token/config)
# ------------------------------------------------------------------
ENV_FILE="${INSTALL_DIR}/etc/orchestrator.env"
if [ ! -f "${ENV_FILE}" ]; then
    TOKEN="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
    umask 077
    cat > "${ENV_FILE}" <<EOF
AGENT_MESH_HOST=0.0.0.0
AGENT_MESH_PORT=${ORCH_PORT}
AGENT_MESH_TOKEN=${TOKEN}
AGENT_MESH_SWEEP_INTERVAL_S=5
AGENT_MESH_OFFLINE_AFTER_S=30
AGENT_MESH_DB_PATH=${INSTALL_DIR}/data/agent-mesh.db
AGENT_MESH_ARTIFACT_DIR=${INSTALL_DIR}/data/artifacts
LOG_LEVEL=INFO
EOF
    chmod 600 "${ENV_FILE}"
    echo "==> Wrote ${ENV_FILE} (random token generated)"
    echo "    read it with: grep AGENT_MESH_TOKEN ${ENV_FILE}"
else
    echo "==> Keeping existing ${ENV_FILE} (token/config preserved)"
    # Backfill data paths for env files written by older installers.
    for kv in \
        "AGENT_MESH_DB_PATH=${INSTALL_DIR}/data/agent-mesh.db" \
        "AGENT_MESH_ARTIFACT_DIR=${INSTALL_DIR}/data/artifacts"; do
        key="${kv%%=*}"
        grep -q "^${key}=" "${ENV_FILE}" || echo "${kv}" >> "${ENV_FILE}"
    done
fi

# ------------------------------------------------------------------
# Launch wrapper + systemd unit
# ------------------------------------------------------------------
cat > "${INSTALL_DIR}/bin/agent-mesh-orchestrator" <<EOF
#!/usr/bin/env bash
set -e
INSTALL_DIR="${INSTALL_DIR}"
cd "\${INSTALL_DIR}/lib/agent-mesh"
set -a
source "\${INSTALL_DIR}/etc/orchestrator.env"
set +a
export PATH="\${INSTALL_DIR}/lib/venv/bin:\$PATH"
exec python -m agent_mesh.orchestrator.main
EOF
chmod +x "${INSTALL_DIR}/bin/agent-mesh-orchestrator"

cat > /etc/systemd/system/agent-mesh-orchestrator.service <<EOF
[Unit]
Description=agent-mesh orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
EnvironmentFile=${INSTALL_DIR}/etc/orchestrator.env
Environment=PATH=${INSTALL_DIR}/lib/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
WorkingDirectory=${INSTALL_DIR}/lib/agent-mesh
ExecStart=${INSTALL_DIR}/bin/agent-mesh-orchestrator
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" 2>/dev/null || true
systemctl daemon-reload
systemctl enable agent-mesh-orchestrator >/dev/null 2>&1 || true
systemctl restart agent-mesh-orchestrator

# ------------------------------------------------------------------
# Edge probe package (opencode bundled)
# ------------------------------------------------------------------
if [ "${BUILD_PROBE}" = "1" ]; then
    echo "==> Building edge probe package"
    # The build script needs the package installer as its source.
    mkdir -p "${INSTALL_DIR}/lib/agent-mesh/data/bootstrap"
    cp -f "${REPO_DIR}/data/bootstrap/install.sh" \
          "${INSTALL_DIR}/lib/agent-mesh/data/bootstrap/install.sh"

    OPENCODE_ARG=()
    [ -n "${OPENCODE_BIN}" ] && OPENCODE_ARG=(--opencode "${OPENCODE_BIN}")

    # Run the repo's build script with the deployed venv (guaranteed PyInstaller),
    # writing the package where the orchestrator serves it from.
    "${INSTALL_DIR}/lib/venv/bin/python" \
        "${REPO_DIR}/scripts/build-agent-bootstrap.py" \
        --output-dir "${INSTALL_DIR}/data/bootstrap" \
        ${OPENCODE_ARG[@]+"${OPENCODE_ARG[@]}"}

    echo "==> Probe package published to ${INSTALL_DIR}/data/bootstrap/"
    ls -lh "${INSTALL_DIR}/data/bootstrap/"
else
    echo "==> Skipped probe build (--no-probe). Publish one later with:"
    echo "    ${INSTALL_DIR}/lib/venv/bin/python ${REPO_DIR}/scripts/build-agent-bootstrap.py --output-dir ${INSTALL_DIR}/data/bootstrap"
fi

echo ""
echo "==> Deployment complete"
echo "    Install dir : ${INSTALL_DIR}"
echo "    Dashboard   : http://<host>:${ORCH_PORT}/"
echo "    Token       : grep AGENT_MESH_TOKEN ${ENV_FILE}"
echo "    Status      : ./deploy/status.sh"
