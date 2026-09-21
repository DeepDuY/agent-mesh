#!/usr/bin/env bash
# Redeploy agent-mesh from the current checkout without changing config/token.
#
# Also republishes any edge probe package found in the repo's data/bootstrap/
# (built in the separate `agent-mesh-edge` repo). Set BUILD_PROBE=1 to build one
# now from an edge checkout (EDGE_REPO_DIR, sibling ../agent-mesh-edge auto-detected).
#
# Usage:
#   ./deploy/redeploy.sh                 # update server + publish existing probe
#   BUILD_PROBE=1 ./deploy/redeploy.sh   # also rebuild the probe (needs edge repo)
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/agent-mesh}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_PROBE="${BUILD_PROBE:-0}"

source "${REPO_DIR}/deploy/common.sh"

# Edge probe repository (separate from this server repo).
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
    --exclude='代码审核报告.md' \
    --exclude='代码审核整改计划.md' \
    "${REPO_DIR}/" "${INSTALL_DIR}/lib/agent-mesh/"

cd "${INSTALL_DIR}/lib/agent-mesh"
ensure_venv "${INSTALL_DIR}/lib/venv" "$(command -v python3.12 || command -v python3)"
# asyncpg>=0.30 has only manylinux_2_28 wheels (unusable on older glibc).
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -e . 'asyncpg<0.30'

# ------------------------------------------------------------------
# Edge probe package
# ------------------------------------------------------------------
mkdir -p "${INSTALL_DIR}/data/bootstrap"
if [ "${BUILD_PROBE}" = "1" ]; then
    if ! find_edge_repo; then
        echo "ERROR: edge probe repository not found (set EDGE_REPO_DIR or clone ../agent-mesh-edge)." >&2
        exit 1
    fi
    EDGE_PYTHON="${EDGE_PYTHON:-${EDGE_REPO_DIR}/.venv/bin/python}"
    if [ ! -x "${EDGE_PYTHON}" ]; then
        echo "ERROR: edge build env not found at ${EDGE_PYTHON} (see deploy/README.md)." >&2
        exit 1
    fi
    echo "==> Rebuilding edge probe package from ${EDGE_REPO_DIR}"
    "${EDGE_PYTHON}" \
        "${EDGE_REPO_DIR}/scripts/build-agent-bootstrap.py" \
        --output-dir "${INSTALL_DIR}/data/bootstrap"
elif [ -n "$(ls -A "${REPO_DIR}/data/bootstrap"/*.tar.gz 2>/dev/null)" ]; then
    echo "==> Publishing existing probe package from ${REPO_DIR}/data/bootstrap/"
    rsync -a --delete "${REPO_DIR}/data/bootstrap/" "${INSTALL_DIR}/data/bootstrap/"
else
    echo "==> No probe package found; server will run but nodes cannot install/upgrade."
    echo "    Build one: BUILD_PROBE=1 ./deploy/redeploy.sh   (needs the edge repo)"
fi

systemctl start agent-mesh-orchestrator

echo "==> Redeploy complete"
