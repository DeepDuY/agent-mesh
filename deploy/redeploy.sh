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

PORT="$(orch_port "${INSTALL_DIR}")"

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

echo "==> Stopping agent-mesh-orchestrator (port ${PORT})"
systemctl stop agent-mesh-orchestrator 2>/dev/null || true

# Confirm the service really stopped. A non-systemd / stale orchestrator can
# keep holding the port; deploying on top of it silently leaves the old process
# serving (the new one then dies with EADDRINUSE). Refuse to proceed.
if can_check_port; then
    for _ in $(seq 1 15); do
        port_listening "${PORT}" || break
        sleep 1
    done
    if port_listening "${PORT}"; then
        echo "" >&2
        echo "ERROR: port ${PORT} is still in use after 'systemctl stop'." >&2
        echo "       The service was NOT stopped successfully; refusing to redeploy." >&2
        echo "       Listener(s) on ${PORT}:" >&2
        port_holders "${PORT}" >&2 || true
        echo "       Suspect process(es):" >&2
        pgrep -af "agent_mesh.orchestrator.main" >&2 || true
        echo "       Stop it (kill/pkill) and re-run this script." >&2
        echo "" >&2
        exit 1
    fi
else
    echo "WARNING: neither 'ss' nor 'lsof' found; cannot confirm port ${PORT} was freed." >&2
fi

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

# ------------------------------------------------------------------
# Back up the SQLite DB before the service (re)applies migrations.
# ------------------------------------------------------------------
DB_TYPE="$(sed -n 's/^AGENT_MESH_DB_TYPE=//p' "${INSTALL_DIR}/etc/orchestrator.env" 2>/dev/null | tail -1)"
DB_TYPE="${DB_TYPE:-sqlite}"
DB_PATH="$(sed -n 's/^AGENT_MESH_DB_PATH=//p' "${INSTALL_DIR}/etc/orchestrator.env" 2>/dev/null | tail -1)"
DB_PATH="${DB_PATH:-${INSTALL_DIR}/data/agent-mesh.db}"

if [ "${DB_TYPE}" = "sqlite" ] && [ -f "${DB_PATH}" ]; then
    BACKUP="${DB_PATH}.bak-$(date +%Y%m%d-%H%M%S)"
    if command -v sqlite3 >/dev/null 2>&1; then
        if sqlite3 "${DB_PATH}" ".backup '${BACKUP}'" 2>/dev/null; then
            echo "==> Backed up SQLite DB to ${BACKUP}"
        else
            echo "WARNING: SQLite backup failed; continuing." >&2
        fi
    else
        if cp -p "${DB_PATH}" "${BACKUP}"; then
            [ -f "${DB_PATH}-wal" ] && cp -p "${DB_PATH}-wal" "${BACKUP}-wal" 2>/dev/null || true
            [ -f "${DB_PATH}-shm" ] && cp -p "${DB_PATH}-shm" "${BACKUP}-shm" 2>/dev/null || true
            echo "==> Backed up SQLite DB to ${BACKUP} (cp; sqlite3 not found)"
        else
            echo "WARNING: SQLite backup failed; continuing." >&2
        fi
    fi
    # Keep only the 10 most recent backups.
    { ls -1t "${DB_PATH}".bak-* 2>/dev/null || true; } | tail -n +11 | xargs -r rm -f || true
fi

# ------------------------------------------------------------------
# Start + confirm it actually came up (bound the port, healthz ok).
# ------------------------------------------------------------------
systemctl reset-failed agent-mesh-orchestrator 2>/dev/null || true
echo "==> Starting agent-mesh-orchestrator (port ${PORT})"
systemctl start agent-mesh-orchestrator

up=0
for _ in $(seq 1 20); do
    if systemctl is-active --quiet agent-mesh-orchestrator \
        && { ! can_check_port || port_listening "${PORT}"; } \
        && { ! can_http || http_ok "http://127.0.0.1:${PORT}/api/healthz"; }; then
        up=1
        break
    fi
    sleep 1
done

if [ "${up}" -ne 1 ]; then
    echo "" >&2
    echo "ERROR: orchestrator did not come up on port ${PORT}." >&2
    echo "       Recent service log:" >&2
    journalctl -u agent-mesh-orchestrator -n 50 --no-pager >&2 2>/dev/null || true
    exit 1
fi

echo "==> Redeploy complete"
