#!/usr/bin/env bash
# Run a single orchestrator container backed by SQLite (quick local/dev mode).
# For a production-style deployment with PostgreSQL, use docker-compose.yml.
#
# Usage:
#   ./docker/run.sh
# Env:
#   AGENT_MESH_IMAGE  image tag           (default agent-mesh:local)
#   CONTAINER_NAME    container name      (default agent-mesh)
#   AGENT_MESH_PORT   host port           (default 8000)
#   DATA_VOLUME       docker volume name  (default agent-mesh-data)
#   AGENT_MESH_TOKEN  shared secret       (generated + printed if unset)
set -euo pipefail

IMAGE="${AGENT_MESH_IMAGE:-agent-mesh:local}"
NAME="${CONTAINER_NAME:-agent-mesh}"
PORT="${AGENT_MESH_PORT:-8000}"
VOLUME="${DATA_VOLUME:-agent-mesh-data}"
TOKEN="${AGENT_MESH_TOKEN:-}"

command -v docker >/dev/null 2>&1 || {
    echo "ERROR: docker not found." >&2
    exit 1
}

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "ERROR: image ${IMAGE} not found. Build it first: ./docker/build.sh" >&2
    exit 1
fi

if [ -z "${TOKEN}" ]; then
    TOKEN="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
    echo "==> Generated AGENT_MESH_TOKEN (save it; callers/nodes need it):"
    echo "    ${TOKEN}"
fi

docker rm -f "${NAME}" >/dev/null 2>&1 || true
docker run -d --name "${NAME}" \
    -p "${PORT}:8000" \
    -v "${VOLUME}:/data" \
    -e AGENT_MESH_TOKEN="${TOKEN}" \
    --restart unless-stopped \
    "${IMAGE}" >/dev/null

echo "==> ${NAME} started."
echo "    Dashboard: http://<host>:${PORT}/"
echo "    Logs:      docker logs -f ${NAME}"
echo "    Data vol:  ${VOLUME}"
