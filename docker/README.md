# 容器部署（Docker / Compose）

本目录集中存放容器相关文件：

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | 镜像定义（REST API + 内置 Web UI，数据在 `/data`，内置 `HEALTHCHECK`） |
| `docker-compose.yml` | 部署模式 A：orchestrator + PostgreSQL |
| `.env.example` | compose 变量示例，复制为 `docker/.env` |
| `build.sh` | 构建镜像（build 模式） |
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
cp docker/.env.example docker/.env   # 设置 AGENT_MESH_TOKEN / POSTGRES_PASSWORD
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f orchestrator
```

- 端口默认 `8000`，用 `AGENT_MESH_PORT` 覆盖；数据在命名卷 `pgdata` / `meshdata`。
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

### 从 systemd 平滑迁移（停机 ≈ 启动时间）

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
