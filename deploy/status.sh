#!/usr/bin/env bash
# Show status and recent logs for agent-mesh services.
set -e

INSTALL_DIR="/opt/agent-mesh"

echo "==> Service status"
systemctl status agent-mesh-orchestrator --no-pager || true
echo ""
systemctl status agent-mesh-edge --no-pager || true

echo ""
echo "==> Recent orchestrator logs"
tail -n 50 "${INSTALL_DIR}/log/orchestrator.log" 2>/dev/null || true

echo ""
echo "==> Recent edge logs"
tail -n 50 "${INSTALL_DIR}/log/edge.log" 2>/dev/null || true

echo ""
echo "==> Ports"
ss -tlnp | grep -E '8000|8001' || true
