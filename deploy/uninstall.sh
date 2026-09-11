#!/usr/bin/env bash
# Uninstall agent-mesh systemd services and remove /opt/agent-mesh.
set -e

echo "==> Stopping and disabling services"
systemctl stop agent-mesh-orchestrator 2>/dev/null || true
systemctl stop agent-mesh-edge 2>/dev/null || true
systemctl disable agent-mesh-orchestrator 2>/dev/null || true
systemctl disable agent-mesh-edge 2>/dev/null || true

rm -f /etc/systemd/system/agent-mesh-orchestrator.service
rm -f /etc/systemd/system/agent-mesh-edge.service
systemctl daemon-reload

rm -rf /opt/agent-mesh

echo "==> Uninstalled"
