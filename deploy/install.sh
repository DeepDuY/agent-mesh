#!/usr/bin/env bash
# Deploy the agent-mesh orchestrator to /opt/agent-mesh with a systemd service,
# then optionally build and publish the edge probe package (opencode bundled).
#
# The edge probe lives in its OWN repository (`agent-mesh-edge`) and is built
# there with its own virtualenv. Point EDGE_REPO_DIR at a checkout (a sibling
# ../agent-mesh-edge is auto-detected) or pass --no-probe and sync a prebuilt
# package from a GitHub Release later. The resulting package is written to
# <INSTALL_DIR>/data/bootstrap/ where the orchestrator serves it.
#
# Usage:
#   ./deploy/install.sh                 # deploy server + build probe
#   ./deploy/install.sh --no-probe      # server only (no edge repo needed)
#   ./deploy/install.sh --opencode /path/to/opencode
#   EDGE_REPO_DIR=/path/to/agent-mesh-edge ./deploy/install.sh
#   OPENCODE_BIN=/path/to/opencode ./deploy/install.sh
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agent-mesh}"
SERVICE_USER="${SERVICE_USER:-root}"
ORCH_PORT="${AGENT_MESH_PORT:-8000}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

BUILD_PROBE=1
OPENCODE_BIN="${OPENCODE_BIN:-}"

# Edge probe repository (separate from this server repo). Auto-detects a sibling
# checkout; override with EDGE_REPO_DIR.
find_edge_repo() {
    if [ -n "${EDGE_REPO_DIR:-}" ] && [ -f "${EDGE_REPO_DIR}/scripts/build-agent-bootstrap.py" ]; then
        return 0
    fi
    local cand
    for cand in "${REPO_DIR}/../agent-mesh-edge" "/opt/agent-mesh-edge"; do
        if [ -f "${cand}/scripts/build-agent-bootstrap.py" ]; then
            EDGE_REPO_DIR="$(cd "${cand}" && pwd)"
            return 0
        fi
    done
    EDGE_REPO_DIR=""
    return 1
}


usage() {
    cat <<EOF
Usage: ./deploy/install.sh [--no-probe] [--opencode PATH] [--edge-repo DIR]
  --no-probe        skip building/publishing the edge probe package
  --opencode PATH   opencode binary to bundle (else auto-detected, or downloaded from GitHub)
  --edge-repo DIR   agent-mesh-edge checkout used to build the probe (else auto-detected)
  --help            show this help
Env: OPENCODE_BIN (same as --opencode), EDGE_REPO_DIR (same as --edge-repo),
     INSTALL_DIR, SERVICE_USER, AGENT_MESH_PORT
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-probe) BUILD_PROBE=0 ;;
        --opencode) OPENCODE_BIN="${2:-}"; shift ;;
        --edge-repo) EDGE_REPO_DIR="${2:-}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

echo "==> Deploying agent-mesh to ${INSTALL_DIR} (source: ${REPO_DIR})"

# ------------------------------------------------------------------
# Pre-flight: fail early with actionable messages
# ------------------------------------------------------------------
fail() { echo "" >&2; echo "ERROR: $*" >&2; echo "" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }

# 1) root (we install a systemd unit and write to /opt)
if [ "$(id -u)" -ne 0 ]; then
    fail "this installer must run as root (installs a systemd service and writes ${INSTALL_DIR}).
  Re-run as: sudo $0"
fi

# 2) required commands
for c in rsync tar; do
    command -v "$c" >/dev/null 2>&1 || fail "missing required command: ${c}
  Debian/Ubuntu : apt-get install -y ${c}
  RHEL/CentOS   : yum install -y ${c}   (or dnf)"
done
command -v systemctl >/dev/null 2>&1 \
    || warn "systemctl not found: the orchestrator will be installed but NOT started as a service."

# 3) Python >= 3.12
PYBIN="$(command -v python3.12 || command -v python3 || true)"
[ -n "${PYBIN}" ] || fail "Python not found. This project requires Python >= 3.12.
  Debian/Ubuntu : apt-get install -y python3.12
  RHEL/CentOS   : yum install -y python3.12
  Or install via pyenv/miniconda and ensure 'python3.12' is on PATH."
"${PYBIN}" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
    || fail "found ${PYBIN} but it is older than Python 3.12 (this project needs >= 3.12).
  Install Python 3.12+ and make sure 'python3.12' resolves to it."

# 4) disk space (~2GB: venv + opencode ~200MB + probe package)
avail_mb="$(df -Pm "${INSTALL_DIR%/}" 2>/dev/null | awk 'NR==2{print $4}')"
[ -n "${avail_mb}" ] || avail_mb="$(df -Pm / 2>/dev/null | awk 'NR==2{print $4}')"
if [ -n "${avail_mb}" ] && [ "${avail_mb}" -lt 2000 ] 2>/dev/null; then
    warn "only ${avail_mb}MB free; building the probe (opencode ~200MB) needs ~2GB. Free space or use --no-probe."
fi

# 5) port conflicts (stop any existing service first so we don't flag ourselves)
systemctl stop agent-mesh-orchestrator 2>/dev/null || true
if command -v ss >/dev/null 2>&1; then
    for p in "${ORCH_PORT}" "$((ORCH_PORT + 1))"; do
        if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"; then
            warn "port ${p} is already in use; the orchestrator may fail to listen (change AGENT_MESH_PORT)."
        fi
    done
fi

# 6) connectivity (pip install always needs it; opencode download needs github)
have_net() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -o /dev/null --max-time 8 "$1" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O /dev/null --timeout=8 "$1" 2>/dev/null
    else
        return 1
    fi
}
if ! have_net https://pypi.org/simple/; then
    warn "cannot reach pypi.org: 'pip install' will fail unless a local mirror is configured."
fi
if [ "${BUILD_PROBE}" = "1" ] && [ -z "${OPENCODE_BIN}" ] \
    && ! command -v opencode >/dev/null 2>&1 && [ ! -x "${HOME}/.opencode/bin/opencode" ]; then
    echo "==> opencode not found locally; it will be downloaded from github.com/sst/opencode"
    have_net https://github.com \
        || warn "cannot reach github.com: opencode download may fail (use --opencode PATH, or --no-probe)."
fi

echo "==> Pre-flight checks passed"

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
    --exclude='代码审核报告.md' \
    --exclude='代码审核整改计划.md' \
    "${REPO_DIR}/" "${INSTALL_DIR}/lib/agent-mesh/"

# ------------------------------------------------------------------
# Virtualenv + dependencies
# ------------------------------------------------------------------
# Some distros ship a Python without ensurepip (so `python -m venv` fails);
# ensure_venv() in common.sh handles that with several fallbacks.
source "${REPO_DIR}/deploy/common.sh"

ensure_venv "${INSTALL_DIR}/lib/venv" "${PYBIN}"

echo "==> Installing dependencies"
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -U pip
# asyncpg>=0.30 has only manylinux_2_28 wheels; on older glibc the source build
# fails. Pin <0.30 which ships a broadly-compatible wheel (see known-issues §18).
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -e "${INSTALL_DIR}/lib/agent-mesh" 'asyncpg<0.30'

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
# Edge probe package (opencode bundled) — built from the separate edge repo
# ------------------------------------------------------------------
if [ "${BUILD_PROBE}" = "1" ]; then
    if ! find_edge_repo; then
        echo "ERROR: edge probe repository not found." >&2
        echo "  The probe is built from the separate 'agent-mesh-edge' repo." >&2
        echo "  Clone it next to this repo (../agent-mesh-edge), pass --edge-repo DIR," >&2
        echo "  set EDGE_REPO_DIR, or re-run with --no-probe and sync a release later." >&2
        exit 1
    fi
    EDGE_PYTHON="${EDGE_PYTHON:-${EDGE_REPO_DIR}/.venv/bin/python}"
    if [ ! -x "${EDGE_PYTHON}" ]; then
        echo "ERROR: edge build env not found at ${EDGE_PYTHON}" >&2
        echo "  Create it in ${EDGE_REPO_DIR}, e.g.:" >&2
        echo "    python3 -m venv .venv && .venv/bin/pip install pyinstaller httpx pydantic pydantic-settings psutil" >&2
        exit 1
    fi
    echo "==> Building edge probe package from ${EDGE_REPO_DIR}"

    OPENCODE_ARG=()
    [ -n "${OPENCODE_BIN}" ] && OPENCODE_ARG=(--opencode "${OPENCODE_BIN}")

    "${EDGE_PYTHON}" "${EDGE_REPO_DIR}/scripts/build-agent-bootstrap.py" \
        --output-dir "${INSTALL_DIR}/data/bootstrap" \
        ${OPENCODE_ARG[@]+"${OPENCODE_ARG[@]}"}

    echo "==> Probe package published to ${INSTALL_DIR}/data/bootstrap/"
    ls -lh "${INSTALL_DIR}/data/bootstrap/"
else
    echo "==> Skipped probe build (--no-probe). Publish one later by re-running:"
    echo "    EDGE_REPO_DIR=/path/to/agent-mesh-edge $0"
    echo "    or sync a published release: ${INSTALL_DIR}/lib/venv/bin/python ${REPO_DIR}/scripts/sync_probe_release.py"
fi

echo ""
echo "==> Deployment complete"
echo "    Install dir : ${INSTALL_DIR}"
echo "    Dashboard   : http://<host>:${ORCH_PORT}/"
echo "    Token       : grep AGENT_MESH_TOKEN ${ENV_FILE}"
echo "    Status      : ./deploy/status.sh"
