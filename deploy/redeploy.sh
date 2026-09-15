#!/usr/bin/env bash
# Redeploy agent-mesh from the current checkout without changing config/token.
#
# Also republishes any edge probe package found in the repo's data/bootstrap/
# (built earlier with scripts/build-agent-bootstrap.py). Set BUILD_PROBE=1 to
# rebuild the probe now (requires opencode + PyInstaller).
#
# Usage:
#   ./deploy/redeploy.sh                 # update server + publish existing probe
#   BUILD_PROBE=1 ./deploy/redeploy.sh   # also rebuild the probe (bundles opencode)
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agent-mesh}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_PROBE="${BUILD_PROBE:-0}"

source "${REPO_DIR}/deploy/common.sh"

echo "==> Redeploying agent-mesh source"

systemctl stop agent-mesh-orchestrator 2>/dev/null || true

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
    --exclude='代码审核报告.md' \
    --exclude='代码审核整改计划.md' \
    --exclude='mcp-server.json' \
    "${REPO_DIR}/" "${INSTALL_DIR}/lib/agent-mesh/"

cd "${INSTALL_DIR}/lib/agent-mesh"
ensure_venv "${INSTALL_DIR}/lib/venv" "$(command -v python3.12 || command -v python3)"
# asyncpg>=0.30 has only manylinux_2_28 wheels (unusable on older glibc).
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -e '.[dev]' 'asyncpg<0.30'

# ------------------------------------------------------------------
# Edge probe package
# ------------------------------------------------------------------
mkdir -p "${INSTALL_DIR}/data/bootstrap"
if [ "${BUILD_PROBE}" = "1" ]; then
    echo "==> Rebuilding edge probe package"
    mkdir -p "${INSTALL_DIR}/lib/agent-mesh/data/bootstrap"
    cp -f "${REPO_DIR}/data/bootstrap/install.sh" \
          "${INSTALL_DIR}/lib/agent-mesh/data/bootstrap/install.sh"
    "${INSTALL_DIR}/lib/venv/bin/python" \
        "${REPO_DIR}/scripts/build-agent-bootstrap.py" \
        --output-dir "${INSTALL_DIR}/data/bootstrap"
elif [ -n "$(ls -A "${REPO_DIR}/data/bootstrap"/*.tar.gz 2>/dev/null)" ]; then
    echo "==> Publishing existing probe package from ${REPO_DIR}/data/bootstrap/"
    rsync -a --delete "${REPO_DIR}/data/bootstrap/" "${INSTALL_DIR}/data/bootstrap/"
else
    echo "==> No probe package found; server will run but nodes cannot install/upgrade."
    echo "    Build one: BUILD_PROBE=1 ./deploy/redeploy.sh"
fi

systemctl start agent-mesh-orchestrator

echo "==> Redeploy complete"
