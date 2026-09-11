#!/usr/bin/env bash
# Start orchestrator in background.
set -e
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"

# Global token: read from data/orchestrator.env, or generate once and persist.
ENV_FILE="./data/orchestrator.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi
if [ -z "$AGENT_MESH_TOKEN" ] || [ "$AGENT_MESH_TOKEN" = "change-me-shared-secret" ] || [ "$AGENT_MESH_TOKEN" = "demo-token" ]; then
    mkdir -p data
    AGENT_MESH_TOKEN="$(python -c "import secrets; print(secrets.token_urlsafe(32))")"
    printf 'AGENT_MESH_TOKEN=%s\n' "$AGENT_MESH_TOKEN" > "$ENV_FILE"
    echo "generated a new AGENT_MESH_TOKEN in $ENV_FILE (keep it secret)"
fi
export AGENT_MESH_TOKEN
export AGENT_MESH_HOST=0.0.0.0
export AGENT_MESH_PORT=8000
pkill -f "agent_mesh.orchestrator.main" || true
sleep 1
nohup uv run python -m agent_mesh.orchestrator.main > /tmp/orch.log 2>&1 &
echo $! > /tmp/orch.pid
echo "orchestrator pid=$(cat /tmp/orch.pid)"
sleep 2
echo "listening:"
ss -tlnp | grep 8000 || true
echo "health (127.0.0.1): $(curl -s http://127.0.0.1:8000/api/healthz)"
echo "dashboard: http://127.0.0.1:8000/"
