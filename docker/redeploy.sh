#!/usr/bin/env bash
# Upgrade a Compose PostgreSQL deployment or a run.sh SQLite container.
# Usage: ./docker/redeploy.sh [--backup-only | --no-build | --rollback BACKUP]
# COMPOSE_FILE defaults to /opt/agent-mesh/docker-compose.yml; if it does not
# exist, the script upgrades CONTAINER_NAME (default: agent-mesh) from run.sh.
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/agent-mesh}"
COMPOSE_FILE="${COMPOSE_FILE:-${INSTALL_DIR}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$(dirname "${COMPOSE_FILE}")/.env}"
BACKUP_DIR="${BACKUP_DIR:-${INSTALL_DIR}/backups}"
CONTAINER_NAME="${CONTAINER_NAME:-agent-mesh}"
ROLLBACK_TAG="${ROLLBACK_TAG:-agent-mesh:previous}"
ACTION="${1:-upgrade}"
BACKUP="${2:-}"

usage() {
    echo "Usage: $0 [--backup-only | --no-build | --rollback BACKUP]" >&2
    echo "Env: INSTALL_DIR COMPOSE_FILE ENV_FILE BACKUP_DIR CONTAINER_NAME ROLLBACK_TAG" >&2
}
fail() { echo "ERROR: $*" >&2; exit 1; }

case "${ACTION}" in
    upgrade|--backup-only|--no-build) [ "$#" -le 1 ] || { usage; exit 2; } ;;
    --rollback) [ "$#" -eq 2 ] || { usage; exit 2; } ;;
    *) usage; exit 2 ;;
esac

command -v docker >/dev/null || fail "docker is required"
command -v python3 >/dev/null || fail "python3 is required for backup/health verification"

# Parse only the keys needed by this script; never source a Compose .env file.
env_value() {
    python3 - "$ENV_FILE" "$1" <<'PY'
import sys
from pathlib import Path
path, key = sys.argv[1:]
if Path(path).is_file():
    for line in Path(path).read_text().splitlines():
        if line.startswith(key + '='):
            value = line.split('=', 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            print(value)
PY
}

if [ -f "$COMPOSE_FILE" ]; then
    MODE=compose
    [ -f "$ENV_FILE" ] || fail "missing Compose env file: $ENV_FILE"
    docker compose version >/dev/null || fail "docker compose is required"
    compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }
    services="$(compose config --services)"
    grep -qx postgres <<< "$services" || fail "Compose file must contain a postgres service"
    grep -qx orchestrator <<< "$services" || fail "Compose file must contain an orchestrator service"
    IMAGE="$(env_value AGENT_MESH_IMAGE)"
    IMAGE="${IMAGE:-agent-mesh:local}"
    PORT="$(env_value AGENT_MESH_PORT)"
    PORT="${PORT:-8000}"
    PG_USER="$(env_value POSTGRES_USER)"
    PG_DB="$(env_value POSTGRES_DB)"
    PG_USER="${PG_USER:-agent_mesh}"
    PG_DB="${PG_DB:-agent_mesh}"
else
    MODE=run
    inspected="$CONTAINER_NAME"
    if [ "$ACTION" = --rollback ] && ! docker container inspect "$inspected" >/dev/null 2>&1; then
        inspected="${CONTAINER_NAME}-previous"
    fi
    docker container inspect "$inspected" >/dev/null 2>&1 || fail "no Compose file or run.sh container found"
    IMAGE="$(docker inspect -f '{{.Config.Image}}' "$inspected")"
    PORT="$(docker inspect -f '{{with index .HostConfig.PortBindings "8000/tcp"}}{{(index . 0).HostPort}}{{end}}' "$inspected")"
    [ -n "$PORT" ] || fail "run.sh container has no published port 8000/tcp"
fi

docker image inspect "$IMAGE" >/dev/null || fail "missing current image $IMAGE"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)-$$"

pg_container() {
    local id
    id="$(compose ps -q postgres)"
    [ -n "$id" ] || fail "postgres service is not running"
    [ "$(docker inspect -f '{{.State.Running}}' "$id")" = true ] || fail "postgres is not running"
    echo "$id"
}

backup_pg() {
    local id tmp out
    id="$(pg_container)"
    out="${BACKUP_DIR}/pg-${STAMP}.dump"
    tmp="${out}.partial"
    if ! docker exec "$id" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$tmp"; then
        rm -f "$tmp"
        fail "pg_dump failed; upgrade cancelled"
    fi
    if [ ! -s "$tmp" ] || ! docker exec -i "$id" pg_restore -l < "$tmp" >/dev/null; then
        rm -f "$tmp"
        fail "PostgreSQL backup is invalid; upgrade cancelled"
    fi
    chmod 600 "$tmp"
    mv "$tmp" "$out"
    echo "==> Database backup: $out"
}

backup_sqlite() {
    local tmp out
    out="${BACKUP_DIR}/sqlite-${STAMP}.db"
    tmp="/data/.redeploy-backup-${STAMP}-$$.db"
    # Python's online backup API includes uncheckpointed WAL changes.
    docker exec "$CONTAINER_NAME" python -c 'import sqlite3,sys; s=sqlite3.connect("/data/agent-mesh.db"); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()' "$tmp" || fail "SQLite backup failed"
    if ! docker cp "${CONTAINER_NAME}:${tmp}" "$out"; then
        docker exec "$CONTAINER_NAME" rm -f "$tmp" || true
        fail "could not copy SQLite backup to host"
    fi
    docker exec "$CONTAINER_NAME" rm -f "$tmp"
    chmod 600 "$out"
    [ -s "$out" ] || fail "SQLite backup is empty"
    python3 - "$out" <<'PY' || fail "SQLite backup integrity check failed"
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'; c.close()
PY
    echo "==> Database backup: $out"
}

# Run-mode stores the old container, including its exact token/config, under a
# different name. Recreate with its mounted data and all AGENT_MESH_* variables.
run_config() {
    docker inspect "$1" | python3 -c '
import json,sys
c=json.load(sys.stdin)[0]
p=c["HostConfig"]["PortBindings"].get("8000/tcp")
m=[x for x in c["Mounts"] if x["Destination"]=="/data"]
if not p or len(m)!=1: sys.exit("expected run.sh port and one /data mount")
x=m[0]; src=x["Name"] if x["Type"]=="volume" else x["Source"]
print(p[0]["HostPort"]); print(src); print(c["HostConfig"]["RestartPolicy"]["Name"] or "unless-stopped")
'
}

recreate_run() {
    local old="$1" envtmp port source restart
    envtmp="$(mktemp)"
    chmod 600 "$envtmp"
    docker inspect "$old" | python3 -c '
import json,sys
c=json.load(sys.stdin)[0]
for item in c["Config"]["Env"]:
    if item.startswith(("AGENT_MESH_", "LOG_LEVEL=")): print(item)
' > "$envtmp"
    mapfile -t cfg < <(run_config "$old")
    port="${cfg[0]}"; source="${cfg[1]}"; restart="${cfg[2]}"
    if ! docker run -d --name "$CONTAINER_NAME" -p "${port}:8000" \
        -v "${source}:/data" --env-file "$envtmp" \
        --restart "$restart" "$IMAGE" >/dev/null; then
        rm -f "$envtmp"
        fail "could not create new container; previous container is $old"
    fi
    rm -f "$envtmp"
}

image_version() {
    docker run --rm --entrypoint python "$IMAGE" -c 'from agent_mesh.shared.constants import SERVER_VERSION; print(SERVER_VERSION)'
}

verify() {
    local expected="$1" id state
    echo "==> Waiting for version $expected on port $PORT"
    for _ in $(seq 1 45); do
        if [ "$MODE" = compose ]; then id="$(compose ps -q orchestrator)";
        else id="$(docker ps -q -f "name=^/${CONTAINER_NAME}$")"; fi
        if [ -n "$id" ]; then
            state="$(docker inspect -f '{{.State.Health.Status}}' "$id")"
            if [ "$state" = healthy ] && python3 - "$PORT" "$expected" <<'PY'
import json, sys, urllib.request
try:
    v=json.load(urllib.request.urlopen('http://127.0.0.1:'+sys.argv[1]+'/api/healthz', timeout=3))
    sys.exit(0 if v.get('status')=='ok' and v.get('db')=='ok' and v.get('version')==sys.argv[2] else 1)
except Exception:
    sys.exit(1)
PY
            then echo "==> Healthy: $expected (database ok)"; return 0; fi
        fi
        sleep 2
    done
    if [ "$MODE" = compose ]; then compose logs --tail 50 orchestrator >&2;
    else docker logs --tail 50 "$CONTAINER_NAME" >&2; fi
    fail "health/version check failed; backup is in $BACKUP_DIR. Restore the DB before running the previous image if its schema changed"
}

if [ "$ACTION" = --rollback ]; then
    [ -s "$BACKUP" ] || fail "rollback requires an existing, nonempty database backup"
    if [ "$MODE" = compose ]; then
        [[ "$BACKUP" == *.dump ]] || fail "expected a pg_dump custom-format .dump"
        docker image inspect "$ROLLBACK_TAG" >/dev/null || fail "missing rollback image $ROLLBACK_TAG"
        id="$(pg_container)"
        docker exec -i "$id" pg_restore -l < "$BACKUP" >/dev/null || fail "invalid PostgreSQL backup"
        compose stop orchestrator
        docker exec "$id" dropdb -U "$PG_USER" --if-exists --force "$PG_DB"
        docker exec "$id" createdb -U "$PG_USER" "$PG_DB"
        docker exec -i "$id" pg_restore -U "$PG_USER" -d "$PG_DB" --exit-on-error --no-owner < "$BACKUP" || fail "restore failed; do not start orchestrator"
        docker tag "$ROLLBACK_TAG" "$IMAGE"
        compose up -d --no-deps --force-recreate orchestrator
    else
        [[ "$BACKUP" == *.db ]] || fail "expected a SQLite .db backup"
        BACKUP="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$BACKUP")"
        python3 - "$BACKUP" <<'PY' || fail "invalid SQLite backup"
import sqlite3, sys
with sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True) as db:
    assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
PY
        old="${CONTAINER_NAME}-previous"
        docker container inspect "$old" >/dev/null || fail "previous container $old not found"
        mapfile -t cfg < <(run_config "$old")
        if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
            docker rm -f "$CONTAINER_NAME" >/dev/null
        fi
        docker run --rm --user 0:0 -v "${cfg[1]}:/data" -v "${BACKUP}:/restore.db:ro" \
            --entrypoint python "$IMAGE" -c 'import os,shutil; shutil.copyfile("/restore.db", "/data/agent-mesh.db"); [os.remove("/data/agent-mesh.db"+s) for s in ("-wal","-shm") if os.path.exists("/data/agent-mesh.db"+s)]'
        docker rename "$old" "$CONTAINER_NAME"
        docker start "$CONTAINER_NAME" >/dev/null
    fi
    if [ "$MODE" = compose ]; then
        verify "$(image_version)"
    else
        verify "$(docker exec "$CONTAINER_NAME" python -c 'from agent_mesh.shared.constants import SERVER_VERSION; print(SERVER_VERSION)')"
    fi
    exit 0
fi

if [ "$MODE" = compose ]; then backup_pg; else backup_sqlite; fi
[ "$ACTION" = --backup-only ] && exit 0

docker tag "$IMAGE" "$ROLLBACK_TAG"
echo "==> Previous image saved as $ROLLBACK_TAG"
if [ "$ACTION" != --no-build ]; then
    "${REPO_DIR}/docker/build.sh" "$IMAGE"
fi

if [ "$MODE" = compose ]; then
    # Only restart the orchestrator. The database remains running throughout.
    compose up -d --no-deps --force-recreate orchestrator
else
    old="${CONTAINER_NAME}-previous"
    docker container inspect "$old" >/dev/null 2>&1 && fail "previous container $old already exists; resolve it before upgrading again"
    docker rename "$CONTAINER_NAME" "$old"
    docker stop "$old" >/dev/null
    recreate_run "$old"
fi
verify "$(image_version)"
echo "==> Upgrade complete; backup retained under $BACKUP_DIR"
