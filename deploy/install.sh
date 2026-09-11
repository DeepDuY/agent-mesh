#!/usr/bin/env bash
# Deploy the agent-mesh orchestrator to /opt/agent-mesh with a systemd service.
# The edge probe is NOT installed here: it is deployed separately on each target
# machine via the bootstrap installer (`GET /api/bootstrap/install.sh`).
set -e

INSTALL_DIR="/opt/agent-mesh"
SERVICE_USER="${SERVICE_USER:-root}"
ORCH_PORT="${AGENT_MESH_PORT:-8000}"
TOKEN="${AGENT_MESH_TOKEN:-$(openssl rand -hex 32)}"

echo "==> Deploying agent-mesh to ${INSTALL_DIR}"

# Stop existing services.
systemctl stop agent-mesh-orchestrator 2>/dev/null || true

# Create directory structure.
mkdir -p "${INSTALL_DIR}"/{bin,lib,etc,log,run,data/artifacts}

# Copy source.
rsync -a --delete \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='data' \
    "$(dirname "$0")/../" "${INSTALL_DIR}/lib/agent-mesh/"

# Create virtual environment if missing.
if [[ ! -d "${INSTALL_DIR}/lib/venv" ]]; then
    echo "==> Creating virtual environment"
    python3.12 -m venv "${INSTALL_DIR}/lib/venv"
fi

# Install dependencies.
echo "==> Installing dependencies"
cd "${INSTALL_DIR}/lib/agent-mesh"
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -U pip
"${INSTALL_DIR}/lib/venv/bin/pip" install -q -e '.[dev]'

# Write orchestrator environment.
cat > "${INSTALL_DIR}/etc/orchestrator.env" <<EOF
AGENT_MESH_HOST=0.0.0.0
AGENT_MESH_PORT=${ORCH_PORT}
AGENT_MESH_TOKEN=${TOKEN}
AGENT_MESH_SWEEP_INTERVAL_S=5
AGENT_MESH_OFFLINE_AFTER_S=30
LOG_LEVEL=INFO
EOF

# Write wrapper script.
cat > "${INSTALL_DIR}/bin/agent-mesh-orchestrator" <<'EOF'
#!/usr/bin/env bash
set -e
INSTALL_DIR="/opt/agent-mesh"
cd "${INSTALL_DIR}/lib/agent-mesh"
source "${INSTALL_DIR}/etc/orchestrator.env"
export PATH="${INSTALL_DIR}/lib/venv/bin:$PATH"
exec python -m agent_mesh.orchestrator.main
EOF

chmod +x "${INSTALL_DIR}/bin/agent-mesh-orchestrator"

# Write systemd service.
cat > /etc/systemd/system/agent-mesh-orchestrator.service <<EOF
[Unit]
Description=agent-mesh orchestrator
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
EnvironmentFile=${INSTALL_DIR}/etc/orchestrator.env
WorkingDirectory=${INSTALL_DIR}/lib/agent-mesh
ExecStart=${INSTALL_DIR}/bin/agent-mesh-orchestrator
StandardOutput=append:${INSTALL_DIR}/log/orchestrator.log
StandardError=append:${INSTALL_DIR}/log/orchestrator.log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Fix permissions.
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"

# Reload systemd and enable service.
systemctl daemon-reload
systemctl enable agent-mesh-orchestrator

echo "==> Deployment complete"
echo "    Install dir: ${INSTALL_DIR}"
echo "    Token: ${TOKEN}"
echo ""
echo "Start service:"
echo "    systemctl start agent-mesh-orchestrator"
echo ""
echo "View log:"
echo "    tail -f ${INSTALL_DIR}/log/orchestrator.log"
echo ""
echo "Note: edge probes are installed separately on target machines via"
echo "    TOKEN=... bash <(curl -fsSL <public_url>/api/bootstrap/install.sh)"
