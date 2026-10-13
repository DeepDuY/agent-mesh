# agent-mesh orchestrator (server). Builds a self-contained image that serves
# the REST API + bundled Web UI. Data (SQLite DB, artifacts, bootstrap package)
# lives under /data, so mount a volume there.
#
# Build:  docker build -t agent-mesh:local .
#   or:   ./deploy/docker-build.sh
# Run:    see docker-compose.yml (PostgreSQL) or deploy/docker-run.sh (SQLite).
#
# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the package (its dependencies + code + bundled web_ui).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# Runtime state lives here (SQLite DB + artifacts + bootstrap). Owned by the
# unprivileged service user so a mounted volume works out of the box.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin agentmesh \
    && mkdir -p /data/artifacts \
    && chown -R agentmesh:agentmesh /data

ENV AGENT_MESH_HOST=0.0.0.0 \
    AGENT_MESH_PORT=8000 \
    AGENT_MESH_DB_PATH=/data/agent-mesh.db \
    AGENT_MESH_ARTIFACT_DIR=/data/artifacts

USER agentmesh
EXPOSE 8000
VOLUME ["/data"]

# GET /api/healthz is public and also reports the DB schema self-check result.
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "agent_mesh.orchestrator.main"]
