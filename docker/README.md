# 容器部署（Docker / Compose）

本目录集中存放容器相关文件：

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | 镜像定义（REST API + 内置 Web UI，数据在 `/data`，内置 `HEALTHCHECK`） |
| `docker-compose.yml` | 部署模式 A：orchestrator + PostgreSQL（已有/新建 PG16 卷） |
| `.env.example` | compose 变量示例，复制为 `docker/.env` |
| `build.sh` | 构建镜像（build 模式） |
| `redeploy.sh` | 备份数据库、升级容器、健康校验及按备份回滚 |
| `run.sh` | 部署模式 B：单容器 SQLite（快速本地/开发） |

> 构建上下文是**仓库根目录**（镜像需要 `pyproject.toml` 与 `src/`）。Docker 只从构建上下文根读取
> `.dockerignore`，所以它保留在仓库根。请始终用 `build.sh` / `docker compose -f docker/...`，不要
> 在 `docker/` 里直接 `docker build .`。

## 构建（build 模式）

```bash
./docker/build.sh                 # 打 agent-mesh:local（并额外打 agent-mesh:<git-sha>）
./docker/build.sh myrepo/am:1.9   # 自定义 tag
```

## 部署模式 A：PostgreSQL（推荐）

```bash
docker volume create agent-mesh-pg-data   # 新安装时只需运行一次
cp docker/.env.example docker/.env       # 设置 AGENT_MESH_TOKEN / POSTGRES_PASSWORD
./docker/build.sh
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f orchestrator
```

- 端口默认 `8000`，用 `AGENT_MESH_PORT` 覆盖。PostgreSQL 使用已有的 PG16 Docker 卷（默认 `agent-mesh-pg-data`，可设 `AGENT_MESH_PG_VOLUME`）；文件、技能、产物和探针包绑定到 `AGENT_MESH_DATA_DIR`（默认 `/opt/agent-mesh/data`）。
- 数据库卷必须先存在，且不能同时挂载到两个运行中的 PostgreSQL 容器。`POSTGRES_PASSWORD` 对已有卷不会重置角色密码；接管旧卷时应填写旧库的密码并用 `AGENT_MESH_POSTGRES_IMAGE` 保持原 PG16 镜像发行版一致（避免 collation 差异）。
- 既有 `data/` 若为 root 属主，orchestrator 默认用 `0:0` 运行以沿用写权限；如新安装数据目录属其他用户，可用 `AGENT_MESH_CONTAINER_USER` 覆盖。探针包位于持久化的 `data/bootstrap/`，服务端升级不会覆盖它。
- 也可在 `docker/` 目录内直接 `docker compose up -d`。

## 部署模式 B：单容器 SQLite（快速本地/开发）

```bash
./docker/run.sh     # 未设 AGENT_MESH_TOKEN 时会生成并打印
```

## 部署模式 C：容器化既有部署（外部 PostgreSQL）

已有 PostgreSQL（例如一个在跑的 `agent-mesh-pg` 容器 / 主机 / 远程库）时，只用容器跑 orchestrator，**不动数据库**：

```bash
cp docker/.env.external-pg.example docker/.env    # 填 AGENT_MESH_PG_DSN + 同一个 AGENT_MESH_TOKEN
docker compose -f docker/docker-compose.external-pg.yml up -d
```

- 使用 host 网络：容器直接用 `127.0.0.1:5432` 连库、并直接占用宿主 `AGENT_MESH_PORT`（无需 ports 映射）。
- `AGENT_MESH_DATA_DIR`（默认 `/opt/agent-mesh/data`）bind 到容器 `/data`，沿用旧的 artifacts/files/skills/bootstrap。
- 默认以 root 运行（兼容 root 属主的数据目录）；可用 `AGENT_MESH_CONTAINER_USER` 覆盖。
- **生产运行时文件不放项目目录**：把 compose + `.env` 放到安装目录（如 `/opt/agent-mesh/docker-compose.yml` +
  `/opt/agent-mesh/.env`）并在那里 `docker compose ... up -d`；本目录的 `docker-compose.external-pg.yml` 作为可复用模板。
  镜像仍由本目录 `build.sh` 从源码构建。

## 从独立 PostgreSQL 容器迁入一体 Compose

已有的 PG16 容器使用 Docker 命名卷时，可以在 **pg_dump 完整备份后** 用新 Compose 接管该卷。现有生产的示例：旧容器 `agent-mesh-pg` 使用 `agent-mesh-pg-data`，原 orchestrator 为独立 PG Compose（8000），数据目录为 `/opt/agent-mesh/data`。

1. 保留旧 token、端口、数据目录，并从旧 `AGENT_MESH_PG_DSN` 取 **现有** 用户名、密码、库名；按 `docker-compose.yml` 和 `.env.example` 写入安装目录的私有运行时文件。设 `AGENT_MESH_PG_VOLUME=agent-mesh-pg-data`，不要复制或初始化旧卷。
2. 提前从旧容器运行 `pg_dump -Fc` 保存到 `/opt/agent-mesh/backups/`（权限仅管理员），验证备份能用 `pg_restore -l` 读取；另保留旧 Compose、`.env` 和旧镜像。
3. 停旧 orchestrator，**再停旧 PG 容器**；确认旧 PG 不再占用卷，然后从安装目录 `docker compose up -d`。新 `postgres` 服务挂载同卷，orchestrator 通过服务名连接并执行 PG 列级升级。
4. `curl -fsS http://127.0.0.1:8000/api/healthz` 应返回 `version=2.0.0`、`db=ok`。PG 的健康接口不返回 `schema_version`（该字段只用于 SQLite）；检查日志、数据数量和关键表列验证迁移。

旧镜像应单独保留标签（例如 `agent-mesh:1.9.0-prod`）；升级脚本每次运行都会更新 `agent-mesh:previous`。若需回退这次迁移，指定 `ROLLBACK_TAG=agent-mesh:1.9.0-prod ./docker/redeploy.sh --rollback /opt/agent-mesh/backups/<迁移前备份.dump>`。首次切换前保存的原 Compose/.env 可用于还原部署拓扑；恢复 PG 数据时确保只有一个容器占用该卷。

## 标准升级与回滚

从当前源码 checkout（如 `main`）执行：

```bash
./docker/redeploy.sh                         # PG Compose：备份→保留旧镜像→构建→重建 orchestrator→验证
./docker/redeploy.sh --backup-only           # 只备份并校验数据库
./docker/redeploy.sh --no-build              # 已有镜像时只重建与校验
./docker/redeploy.sh --rollback /opt/agent-mesh/backups/pg-YYYYMMDD-HHMMSS-PID.dump
```

- 默认读取 `/opt/agent-mesh/docker-compose.yml` 与旁边的 `.env`，仅支持包含 `postgres`、`orchestrator` 两个服务的一体 Compose。通过 `COMPOSE_FILE`、`ENV_FILE`、`INSTALL_DIR`、`BACKUP_DIR` 覆盖路径。`redeploy.sh` 不修改数据库配置、token、PG 容器或探针包。
- 没有 Compose 文件时，脚本支持 `run.sh` 的单容器 SQLite：用 Python 在线备份 API 获取一致性快照，保留原容器为 `<CONTAINER_NAME>-previous`，沿用原 token、数据卷、端口和重启策略。可用 `CONTAINER_NAME` 覆盖默认 `agent-mesh`；同一机器还存在生产 Compose 时，另设 `COMPOSE_FILE` 指向不存在的文件以选择单容器模式。
- 备份位于 `${BACKUP_DIR:-/opt/agent-mesh/backups}`；PG 使用 custom dump，SQLite 使用 `.db` 文件。旧镜像标记为 `agent-mesh:previous`（可用 `ROLLBACK_TAG` 改）。**回滚必须提供对应升级前的数据库备份**：PG 回滚会停 orchestrator、重建数据库并从 dump 恢复；SQLite 回滚会恢复数据库文件及之前的容器。回滚会丢失备份之后产生的新数据；保存备份与旧镜像直至确认升级稳定。
- 健康检查要求容器 healthy、`/api/healthz` 的版本与新镜像一致且 `db=ok`。失败时打印日志与备份位置；由于迁移可能已写库，不会自动用旧镜像启动。脚本不替代 `deploy/redeploy.sh` 的 systemd 部署流程。

## 从 systemd 平滑迁移（停机 ≈ 启动时间）

```bash
./docker/build.sh                                             # 先构建（不停机）
systemctl stop agent-mesh-orchestrator                        # 停机开始
systemctl disable agent-mesh-orchestrator                     # 避免与容器抢端口/开机自启
docker compose -f docker/docker-compose.external-pg.yml up -d
until curl -fsS localhost:8000/api/healthz | grep -q '"db":"ok"'; do sleep 1; done   # 等就绪
```

回滚：

```bash
docker compose -f docker/docker-compose.external-pg.yml down
systemctl enable --now agent-mesh-orchestrator
```

## 说明

- 健康检查：`GET /api/healthz` 返回 `{"status":"ok","version":...,"db":"ok"}`；`db` 字段来自启动时的数据库
  schema 自检，`db != ok` 表示迁移/升级未完整应用（服务会拒绝启动，见 `docs/orchestrator.md`）。
- 镜像内置 `HEALTHCHECK`：`docker inspect --format '{{.State.Health.Status}}' <容器>`。
- 配置通过环境变量（`AGENT_MESH_*`，见 `config.py`）。
- 探针包：放进挂载的 `/data/bootstrap/`（或看板 / `POST /api/bootstrap/sync`），节点即可安装/升级。
- 本机部署（systemd、非容器）见 [`../deploy/README.md`](../deploy/README.md)。
