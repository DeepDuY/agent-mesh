#!/usr/bin/env bash
# Build the agent-mesh orchestrator container image.
#
# Usage:
#   ./deploy/docker-build.sh                 # tags agent-mesh:local
#   ./deploy/docker-build.sh myrepo/am:1.9   # custom tag
#
# Also tags with the current source revision (agent-mesh:<short-sha>) when the
# working tree is a git checkout, which is handy for pinning / rollbacks.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-agent-mesh:local}"

command -v docker >/dev/null 2>&1 || {
    echo "ERROR: docker not found." >&2
    exit 1
}

echo "==> Building ${TAG} from ${REPO_DIR}"
docker build -t "${TAG}" "${REPO_DIR}"

if rev="$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null)"; then
    docker tag "${TAG}" "agent-mesh:${rev}"
    echo "==> Also tagged agent-mesh:${rev}"
fi

echo "==> Built ${TAG}"
echo "    Run (PostgreSQL): cp .env.example .env && docker compose up -d"
echo "    Run (SQLite):     ./deploy/docker-run.sh"
