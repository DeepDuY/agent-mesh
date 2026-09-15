# 一键安装、配置项与启动部署

> 来源：原 DESIGN.md §13 / §14 / §17。

## 1. 一键安装（bootstrap）

### 1.1 安装包构建（orchestrator 端）

```bash
uv run --no-sync python scripts/build-agent-bootstrap.py
```

> 需在当前 venv 已安装 `pyinstaller`（dev extra：`uv pip install pyinstaller`）。用 `--no-sync` 避免 `uv` 重新同步环境——本机 glibc < 2.28 时 asyncpg 0.31.0 的 `manylinux_2_28` wheel 不可用，`uv run` 会回退源码编译而失败（2026-09-01 发现）。

> ⚠️ **改了探针代码（`src/agent_mesh/edge/**`）必须重建**：先升 `shared/constants.py:VERSION` 再跑本命令，否则在线节点不会拿到新代码（见 [docs/README.md 变更规范 §B](./README.md#b-修改探针edge-下的代码)）。

产物（写到 `data/bootstrap/`）：
- `agent-mesh-agent-{linux|darwin}-{x64|arm64}.tar.gz`：PyInstaller 构建的 `agent-mesh-edge` 单二进制 + `opencode` 二进制 + 包内 `install.sh` + `VERSION` + `MANIFEST.json`（version + 各文件 size/sha256，升级时据此跳过未变化成员）。
- `install.sh`：包内安装脚本的副本，供直接 curl。
- `VERSION`：版本清单（= `shared/constants.py:VERSION`），升级比对用。

`data/bootstrap/install.sh` 是包内安装脚本的唯一真源（构建脚本直接复制，不再动态生成）。流程：
1. 读取 `INSTALL_DIR`（默认 `/opt/agent-mesh-agent`）、`ORCHESTRATOR_URL`、`TOKEN`（缺失即报错退出）、`EDGE_ALIAS`、`EDGE_LLM_*`。
2. **前置检查**：非 root 且 systemd 存在时提示跳过 service；磁盘空间不足 500MB 报错；`$INSTALL_DIR` 已存在时要求 `FORCE_REINSTALL=1`（否则退出）。
3. 复制二进制到 `$INSTALL_DIR/bin/`，`agent-mesh-edge.bin` 为真实二进制，外层 `agent-mesh-edge` 为 wrapper（`source etc/edge.env` 后 `exec`）。wrapper 含两阶段升级回滚逻辑（`etc/upgrading` + `etc/upgrade-started` 标记 → 升级时先备份 `.bin.old` 再替换新二进制，见 [auth-security.md §8](./auth-security.md#8-agent-自升级)）。
4. 写 `$INSTALL_DIR/etc/edge.env`（含 `EDGE_AGENT_ID`、`EDGE_ORCHESTRATOR_URL`、`EDGE_TOKEN`、`EDGE_LLM_*` 等）。
5. 有 systemd 则写 `/etc/systemd/system/agent-mesh-edge.service` 并 `daemon-reload` + `enable` + **`start`（安装后自动启动）**；macOS 则写 LaunchDaemon plist 并 `launchctl load/start`。

### 1.2 Web 看板安装命令（运行时生成）

`GET /api/bootstrap/install.sh`（需用户 token）动态生成**外层安装脚本**（与包内 install.sh 不同）：

```
1. OS=$(uname -s); ARCH=$(uname -m) → x64/arm64
2. 校验 $TOKEN 环境变量存在
3. curl 下载 http://<base_url>/api/bootstrap/agent-mesh-agent-<os>-<arch>.tar.gz
4. 解压并执行包内 install.sh，
   传入 INSTALL_DIR / ORCHESTRATOR_URL / TOKEN / EDGE_ALIAS
   （**不再内嵌任何 LLM 配置**：新节点注册后由心跳 config-sync 下发，见 auth-security.md §9）
```

Web 看板「安装新节点」在 Config 页面设置 **Public URL** 后生成：

```bash
TOKEN='<token>' bash <(curl -fsSL -H "Authorization: Bearer <token>" <public_url>/api/bootstrap/install.sh)
```

> **不再提示填写别名**（v1.4.1 起 UI 已移除）：`EDGE_ALIAS` 只覆盖节点 `agent_id`（安装脚本 `AGENT_ID`），**并非** `agents.alias` 字段，改 DB 别名请用节点详情弹窗的「修改别名」。

节点启动后即自动以 device_id（machine-id）注册上线。

## 2. 配置项

### 2.1 orchestrator（`AGENT_MESH_` 前缀，OrchestratorConfig）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGENT_MESH_HOST` | `0.0.0.0` | 监听地址（REST/Web） |
| `AGENT_MESH_PORT` | `8000` | 监听端口 |
| `AGENT_MESH_TOKEN` | `""`（**生产必设**） | 全局 token；空值时禁用 global 身份（仅 user/agent token 可用），启动打印告警 |
| `AGENT_MESH_PUBLIC_URL` | `""` | 对外 URL（安装脚本用，可留空） |
| `AGENT_MESH_WORKERS` | `os.cpu_count()` | 预留；当前**不生效**（`uvicorn.Server.serve()` 忽略 `workers`，实际始终单进程） |
| `AGENT_MESH_DB_TYPE` | `sqlite` | 存储后端：`sqlite`（默认）或 `pg` |
| `AGENT_MESH_DB_PATH` | `./data/agent-mesh.db` | SQLite 路径 |
| `AGENT_MESH_PG_DSN` | `""` | PostgreSQL DSN（`db_type=pg` 时优先，如 `postgresql://u:p@host/db`） |
| `AGENT_MESH_PG_HOST` | `127.0.0.1` | PG 主机 |
| `AGENT_MESH_PG_PORT` | `5432` | PG 端口 |
| `AGENT_MESH_PG_USER` | `agent_mesh` | PG 用户 |
| `AGENT_MESH_PG_PASSWORD` | `""` | PG 密码 |
| `AGENT_MESH_PG_DATABASE` | `agent_mesh` | PG 数据库名 |
| `AGENT_MESH_ARTIFACT_DIR` | `./data/artifacts` | 产物目录 |
| `AGENT_MESH_ARTIFACT_MAX_SIZE_MB` | `50` | 单文件上限 |
| `AGENT_MESH_ARTIFACT_MAX_TOTAL_MB` | `200` | 总量上限 |
| `AGENT_MESH_SWEEP_INTERVAL_S` | `5` | 清扫间隔（秒） |
| `AGENT_MESH_OFFLINE_AFTER_S` | `15` | 心跳超时判离线（秒）。⚠️ `deploy/install.sh` 生成的 `orchestrator.env` 会覆盖为 `30` |
| `AGENT_MESH_SESSION_TTL_S` | `86400` | 登录 session token 有效期（秒） |

### 2.2 边沿 Agent（`EDGE_` 前缀，EdgeConfig）

> **PostgreSQL**（v1.4.0）：统一连接层 `store/connection/`（`base.py`/`sqlite.py`/`pg.py`）的 `PostgresDatabase` 按 `os.getpid()` **惰性建 asyncpg 池**，为将来多 worker 预留（当前实际单进程运行，见 architecture.md §1.3）。
>
> **从 SQLite 迁移到 PostgreSQL**：
> 1. 启动 PostgreSQL 并建库/用户（`agent_mesh`）。
> 2. 停止 orchestrator（`systemctl stop agent-mesh-orchestrator`）。
> 3. 迁移数据：`python scripts/migrate_sqlite_to_pg.py --sqlite <sqlite.db> --pg-dsn 'postgresql://agent_mesh:PASS@host/db'`。⚠️ 当前脚本迁移 users/agents/agent_users/tasks/queue/results/artifacts/files/skills/settings/logs/migrations + 序列同步；**尚未覆盖 `teams`/`team_members`/`templates`/`task_events` 与 `tasks.user_id/team_id`**（迁移 015 后仍读取已删除的 `allowed_tools` 列，会报错）——待修复，见 [known-issues.md §4](./known-issues.md)。
> 4. 在 `orchestrator.env` 加 `AGENT_MESH_DB_TYPE=pg` + `AGENT_MESH_PG_DSN`，启动服务。
> 5. 回滚：删除两行 PG 配置重启即回 SQLite（原 DB 文件保留）。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EDGE_AGENT_ID` | hostname | 显示名 |
| `EDGE_ORCHESTRATOR_URL` | `http://127.0.0.1:8000` | orchestrator base URL |
| `EDGE_TOKEN` | `""`（必设） | 与 orchestrator 的 `AGENT_MESH_TOKEN` 一致 |
| `EDGE_HEARTBEAT_S` | `3` | 心跳间隔 |
| `EDGE_RUNTIME` | `opencode` | 执行运行时（opencode/claude） |
| `EDGE_WORKDIR` | `.` | 默认工作目录基目录（**任务未指定 workdir 时**落到 `<EDGE_WORKDIR>/tasks/<task_id>/` 独立子目录，v1.4.0 并发隔离；显式指定 workdir 时不用它，见 task-and-execution.md §3.3）。⚠️ 包内 `install.sh` 会写入 `${INSTALL_DIR}/work` |
| `EDGE_INSTALL_DIR` | `/opt/agent-mesh-agent` | 安装目录（machine-id 持久化位置） |
| `EDGE_LLM_API_KEY` | `""` | LLM API key |
| `EDGE_LLM_BASE_URL` | `""` | LLM base URL |
| `EDGE_LLM_MODEL` | `""`（**必配**） | 默认模型（网关真实完整 id；空且任务未传 `model` 时拒绝 llm 任务） |
| `EDGE_LLM_MODELS` | `""` | 可用模型列表（逗号/换行；config-sync 下发） |
| `EDGE_SYSTEM_PROMPT` | `""` | 节点级 system prompt（config-sync 下发） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

> `settings` 表中的 `public_url` / `llm_api_key` / `llm_base_url` / `llm_model` / `llm_models` / `default_permission` 可通过 `/api/settings` 读写（**全局 `system_prompt` 已移除**，提示词改为节点级 `agents.system_prompt` + 模板 `templates.system_prompt`）。其中 **LLM 配置不再内嵌进安装脚本**，节点注册后经心跳 config-sync 下发（见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)）。`llm_models` 是模型唯一来源（无硬编码），`llm_model` 无默认值（未配置则 llm 任务被拒绝）。看板配置页保存时校验响应状态，失败会显示具体错误、不再误报"已保存"。

## 3. 启动与部署

**orchestrator（开发）**：
```bash
./scripts/start-orchestrator.sh
# 或
uv run python -m agent_mesh.orchestrator.main
```
> `start-orchestrator.sh` 从 `data/orchestrator.env` 读取 `AGENT_MESH_TOKEN`；文件不存在或 token 为占位/默认值时，首次启动自动生成随机 token 并持久化到该文件（不再硬编码 `demo-token`）。生产用 `deploy/install.sh` 部署，token 同样放在 `orchestrator.env`。

**边沿 agent（开发）**：
```bash
EDGE_AGENT_ID=client EDGE_ORCHESTRATOR_URL=http://127.0.0.1:8000 \
  uv run python -m agent_mesh.edge.agent
```

**生产部署**：`deploy/install.sh` 只安装 **orchestrator** 到 `/opt/agent-mesh`（systemd 管理 `agent-mesh-orchestrator`）。⚠️ 服务日志走 systemd journal（`journalctl -u agent-mesh-orchestrator`），**不会**写入 `log/orchestrator.log`（见 [known-issues.md §13](./known-issues.md)）。**edge 探针不经此脚本安装**——单独在目标机器上用 bootstrap（`GET /api/bootstrap/install.sh`）安装到 `/opt/agent-mesh-agent`。

**Web 看板**：`http://<host>:8000/`

> ⚠️ MCP 通道（SSE :8001）与 `scripts/mcp_bridge.py` 已整体移除。外部主 Agent 一律通过 REST（`http://<host>:8000/api/*`，`Authorization: Bearer <用户 token>`）接入，见 [protocol.md §3.3](./protocol.md)。
