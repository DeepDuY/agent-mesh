#!/usr/bin/env bash
# Redeploy agent-mesh from current source without changing config/token.
set -e

INSTALL_DIR="/opt/agent-mesh"

echo "==> Redeploying agent-mesh source"

systemctl stop agent-mesh-orchestrator 2>/dev/null || true

rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='data' \
    "$(dirname "$0")/../" "${INSTALL_DIR}/lib/agent-mesh/"

cd "${INSTALL_DIR}/lib/agent-mesh"
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -e '.[dev]'

systemctl start agent-mesh-orchestrator

echo "==> Redeploy complete"
