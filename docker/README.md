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

## 说明

- 健康检查：`GET /api/healthz` 返回 `{"status":"ok","version":...,"db":"ok"}`；`db` 字段来自启动时的数据库
  schema 自检，`db != ok` 表示迁移/升级未完整应用（服务会拒绝启动，见 `docs/orchestrator.md`）。
- 镜像内置 `HEALTHCHECK`：`docker inspect --format '{{.State.Health.Status}}' <容器>`。
- 配置通过环境变量（`AGENT_MESH_*`，见 `config.py`）。
- 探针包：放进挂载的 `/data/bootstrap/`（或看板 / `POST /api/bootstrap/sync`），节点即可安装/升级。
- 本机部署（systemd、非容器）见 [`../deploy/README.md`](../deploy/README.md)。
