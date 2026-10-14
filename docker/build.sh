#!/usr/bin/env bash
# Build the agent-mesh orchestrator container image.
#
# Usage:
#   ./docker/build.sh                 # tags agent-mesh:local
#   ./docker/build.sh myrepo/am:1.9   # custom tag
#
# Build context is the repository root (this script's parent). Also tags with
# the current source revision (agent-mesh:<short-sha>) when in a git checkout,
# which is handy for pinning / rollbacks.
set -euo pipefail

DOCKER_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${DOCKER_DIR}/.." && pwd)"
TAG="${1:-agent-mesh:local}"

command -v docker >/dev/null 2>&1 || {
    echo "ERROR: docker not found." >&2
    exit 1
}

echo "==> Building ${TAG} (context: ${REPO_DIR})"
docker build -f "${DOCKER_DIR}/Dockerfile" -t "${TAG}" "${REPO_DIR}"

if rev="$(git --git-dir="${REPO_DIR}/.git" --work-tree="${REPO_DIR}" rev-parse --short HEAD 2>/dev/null)"; then
    docker tag "${TAG}" "agent-mesh:${rev}"
    echo "==> Also tagged agent-mesh:${rev}"
fi

echo "==> Built ${TAG}"
echo "    PostgreSQL: docker volume create agent-mesh-pg-data  # first install only"
echo "                cp docker/.env.example docker/.env  # set token and password"
echo "                docker compose -f docker/docker-compose.yml up -d"
echo "    SQLite:     ./docker/run.sh"
