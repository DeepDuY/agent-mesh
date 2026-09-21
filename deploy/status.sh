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
# Logs go to the systemd journal (install.sh sets StandardOutput=journal);
# fall back to a file only if the unit/journal is unavailable.
journalctl -u agent-mesh-orchestrator -n 50 --no-pager 2>/dev/null \
    || tail -n 50 "${INSTALL_DIR}/log/orchestrator.log" 2>/dev/null || true

echo ""
echo "==> Recent edge logs"
journalctl -u agent-mesh-edge -n 50 --no-pager 2>/dev/null \
    || tail -n 50 "${INSTALL_DIR}/log/edge.log" 2>/dev/null || true

echo ""
echo "==> Ports"
ss -tlnp | grep -E '8000|8001' || true
