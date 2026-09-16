# agent-mesh

一个通用的多 Agent 远程协同执行框架。

- **主 Agent**：你正在使用的 LLM Agent（DeepChat、OpenCode、Claude 等）
- **orchestrator**：独立调度器，负责任务队列、边沿心跳、任务分发
- **边沿 Agent**：部署在目标机器上的轻量守护进程，用本机 opencode 执行任务

## 一键部署（推荐）

在服务器上（需要 root）：

```bash
git clone https://github.com/DeepDuY/agent-mesh.git
cd agent-mesh
sudo ./deploy/install.sh
```

脚本会自动：部署 orchestrator 到 `/opt/agent-mesh`（systemd 开机自启）→ 生成随机 token → 设置数据目录 → **构建探针安装包（含 opencode）并发布到 `/opt/agent-mesh/data/bootstrap/`**。

之后：打开 `http://<服务器IP>:8000/`（默认 `admin/admin`，首登改密）→ 配置页填 Public URL → 节点页「+ 安装新节点」把探针装到目标机器。

常用参数：

```bash
sudo ./deploy/install.sh --no-probe                 # 只装服务端，不构建探针
sudo ./deploy/install.sh --opencode /path/opencode  # 指定本机 opencode（否则自动下载）
```

详见 [deploy/README.md](deploy/README.md)。

## 核心特性

- 主 Agent 通过 **REST API** 控制 orchestrator
- **单进程架构**：FastAPI（:8000）与后台 sweeper 同一事件循环；`AGENT_MESH_WORKERS` 为预留项，当前不生效（见 docs/known-issues.md）
- **存储层可插拔**：默认 SQLite（WAL 模式），可选 PostgreSQL（`AGENT_MESH_DB_TYPE=pg`）
- 边沿 Agent 通过 **REST** 心跳拉取任务、提交结果
- 任务双模式：`command`（shell 原样执行，不走 LLM）和 `llm`（opencode 自然语言任务）
- **LLM 会话复用**：llm 任务完成后回传 opencode `session_id`；派发新任务时可携带 `session_id`（选填）续用上次会话
- **技能库**：管理端上传技能 zip（提取 SKILL.md 元数据）、边沿/主 Agent 通过摘要接口浏览、按需下载使用；llm 提示词内置技能库使用指引，由 agent 自主决定是否取用
- **文件库**：主 Agent 先上传文件（multipart，返回 `file_id` + `md5`，同内容去重），派发任务时通过 `attachments` 引用；探针执行前自动下载到工作目录并按 md5 校验，下载失败任务标记失败（`attachment download failed`）
- **资源指标**：心跳上报探针版本、CPU、内存使用率，节点详情页可视化展示
- **多任务并发执行**：每个节点默认最多同时执行 2 个任务（`max_concurrent` 可在配置页调整，随心跳下发）；未显式指定 `workdir` 的任务自动落到独立子目录 `<EDGE_WORKDIR>/tasks/<task_id>/`
- **LLM 实时执行输出**：边沿把 opencode 实时输出（文本/错误/完成事件）流式上报，任务详情页实时展示 `GET /api/tasks/{id}/logs`（增量 `?after_id=`），LLM 任务全程可控
- **任务终止**：REST / Web 看板均可取消任务（排队/执行中/已分配），执行中的子进程会被终止
- 产物按模式精准收集：command 只收新增文件，llm 只收声明的 artifacts
- 节点以 **数字 id + device_id（machine-id）** 唯一标识，重复上线只更新不重复注册
- 任务状态机：`queued → assigned → working → completed/failed/timed_out`，另有 `cancelled`（人为终止）与 `denied`（派发被权限策略拦截、从未执行，仍记录在任务列表便于追溯）
- 内置中文 Web 看板（节点/任务/配置），任务列表支持分页、**模糊搜索、状态下拉、模式下拉、开始时间区间（之后/之间/之前）**、弹窗详情、一键终止、批量删除（当前页/跨页全选）、带鉴权的产物下载；文件库支持搜索与**批量删除/批量打包下载**；前端结构拆分为 html/css/js
- 用户账号系统：创建用户时**随机生成 API token**（SHA-256 哈希入库，仅返回一次，可设有效期），用于 REST；登录返回短期 session token 供 Web/脚本使用；用户可在「个人中心」自助生成/轮换 token（留空有效期=永久）；默认账号 `admin/admin`（**务必首登后在「个人中心」修改密码**）；admin 可在看板「用户」页创建/删除用户、重置密码、轮换 token
- **Agent 自升级**：看板/`POST /api/agents/{id}/upgrade` 下发升级指令，节点空闲时自动下载新版安装包、原子替换二进制（`.bin.old` 备份）、失败自动回滚、重启生效
- **LLM 配置同步**：配置页保存 LLM 配置（或设置节点级 `llm_config`）后 `config_version` 自增，通过心跳推送给所有在线节点，边沿自动更新并持久化到 `edge.env`（节点级配置优先）
- **模型列表可配置**：`settings.llm_models`（配置页「可用模型列表」，每行一个网关真实 id）是唯一模型来源，边沿据此生成 opencode 模型表（**无硬编码**）；派发 llm 任务时 `model` 会按列表校验，未配置默认模型则拒绝执行
- **节点描述与角色设定**：每个节点可设 `description`（节点自身用途说明）；`effective_description` = 节点描述优先、否则取绑定模板的 `node_description`（模板专用于「节点描述」的字段，与模板自身的「说明」`description` 区分开），主 Agent 据此选节点。节点级 `system_prompt` 注入该节点每个 llm 任务提示词顶部（**仅管理员**，页面可编辑）
- **节点模板**：可复用的节点配置（`system_prompt` / 默认 `llm_model` / **权限 `permission`** / `node_description` 节点描述 / `description` 说明）；节点**引用式绑定**（`agents.template_id`），改模板自动同步到所有绑定节点。生效顺序：模型 `节点 > 模板 > 全局默认`，提示词按 `内置 + 节点 + 模板` 拼接，节点描述 `节点 > 模板`
- **模板/权限管理只通过页面**：模板读/写与节点改绑模板接口**已从 API 移除**（`require_ui_user`：需页面专用头 `X-Agent-Mesh-UI`，否则 404），任何人都无法用 curl 等外部调用，只能在 Web 管理平台操作；admin 管理全部模板，非 admin 只能管理自己或本团队的模板。节点级 `llm_config`/`system_prompt`、全局 `settings` 要求 `admin` 角色。主 Agent（`list_agents`）只能读到 `template_name` 与 `effective_description`，**不会拿到模板的具体配置**，防止自我提权
- **多租户（团队/组 + 资源归属）**：`teams`/`team_members`（**一个用户至多属一个团队**）；节点 `access` JSON 声明可操作的团队/用户（admin 恒旁路，安装节点的用户及其团队**自动加入**）；任务按 `user_id`/`team_id`（**由 token 自动生成**）过滤——可见 `自己 + 自己团队`（无团队则仅自己），并记录/展示 `dispatched_by`（派发者用户名，任务列表可见「用户」列、可按用户名搜索）；文件按创建者隔离。admin 在「团队」页与节点「访问权限」维护
- **执行权限（模板级，llm + command 共用）**：采用 OpenCode `permission` 规格（`allow|ask|deny` + 命令 glob），内置 `build`/`plan`/`readonly` 三个模板；未绑定模板的节点用全局 `default_permission`（默认 `readonly`）。llm 模式交由 opencode 强制，command 模式由 edge 在 `bash -c` 前求值；拒绝与关键动作写入 `task_events` 审计。**注意：command 匹配器只防误操作，非安全边界**
- 数据持久化（SQLite/PostgreSQL），重启不丢失
- Agent 一键安装脚本（PyInstaller 单二进制 + **内置 opencode**，目标机无需外网）；构建机本机无 opencode 时自动从官方 GitHub releases 下载
- 节点删除：一键下发卸载任务，自动清理安装目录与 systemd 服务

## 快速开始

### 1. 环境要求

- **Python 3.12+**（部署脚本会校验；若该 Python 缺 `ensurepip`，脚本自动用 `get-pip.py` 兜底）
- 部署为 systemd 服务需 **root**；依赖 `rsync`、`tar`、`systemctl`
- 构建探针包需联网（pip 源；本机无 opencode 时会从 `github.com/sst/opencode` 下载）
- **目标边沿机器无需安装 opencode**（已打包进探针）
- 推荐用 `uv` 管理开发依赖；下方 REST 示例用 `jq` 解析 JSON（可选）

### 2. 本地开发安装

```bash
cd <repo>
export PATH="$HOME/.local/bin:$PATH"
uv sync
```

### 3. 启动 orchestrator

```bash
./scripts/start-orchestrator.sh
```

默认监听：
- REST/Web：`http://0.0.0.0:8000`

> 当前为**单进程**运行：`AGENT_MESH_WORKERS>1` 不生效（`uvicorn.Server.serve()` 忽略 `workers`），多 worker 待修复。
> 使用 PostgreSQL：`AGENT_MESH_DB_TYPE=pg AGENT_MESH_PG_DSN='postgresql://user:pass@host/db'`（统一连接层 `store/connection/` 按进程惰性建 asyncpg 池）。
> ⚠️ 一律用 `uv run --no-sync`：直接 `uv run` 会重新解析依赖并尝试源码编译 `asyncpg`，在旧 glibc（<2.28）上会失败。

### 4. 启动 edge agent

```bash
EDGE_AGENT_ID=client EDGE_ORCHESTRATOR_URL=http://127.0.0.1:8000 \
  uv run --no-sync python -m agent_mesh.edge.agent
```

> edge 的 LLM 配置通过 orchestrator 配置页/`PATCH /api/settings` 下发（心跳 config-sync，落盘 `edge.env`）；也可用 `EDGE_LLM_*` 环境变量预置。密钥不写进代码。

### 5. 下发任务

**REST 方式：**

```bash
# 1. 登录获取 session token（短期有效，用于 REST / Web）
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r .token)

# 2. 查在线节点，拿到数字 id
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/agents

# 3. 下发 command 任务（直接跑 shell 命令，不走 LLM）
curl -s -X POST http://127.0.0.1:8000/api/tasks/dispatch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": 1,
    "mode": "command",
    "instruction": "echo hello && uname -a",
    "timeout_s": 120,
    "workdir": "/tmp"
  }'

# 4. 下发 llm 任务（自然语言，走 opencode）
curl -s -X POST http://127.0.0.1:8000/api/tasks/dispatch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": 1,
    "mode": "llm",
    "instruction": "创建 /tmp/hello.txt 写入 hello world",
    "timeout_s": 120,
    "workdir": "/tmp"
  }'
```

> `agent_id` 可传数字 id、device_id（machine-id）或显示名。

**复用 LLM 会话（选填）**：先查上次任务的 `result.session_id`，再在派发时带上 `session_id`，该节点 opencode 会续用上次会话执行：

```bash
# 1. 查上次任务结果里的 session_id
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/tasks/t-xxxxx | jq .task.result.session_id

# 2. 新任务追加 session_id 续用会话
curl -s -X POST http://127.0.0.1:8000/api/tasks/dispatch \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "agent_id": 1, "mode": "llm",
    "instruction": "继续上次的工作，把 hello.txt 改成 hi",
    "session_id": "ses_xxxxx"
  }'
```

任务完成后的 `result.session_id` 即为本次实际使用的会话 ID。

> 如何拿到用户 API token：首次启动 orchestrator 时日志会打印一次 admin 的 token；或通过 `POST /api/auth/users`（admin）创建用户 / `POST /api/auth/users/{username}/token` 轮换 token 获取；用户也可在 Web「个人中心」自助生成（`POST /api/auth/token`）。API token 只显示一次，请妥善保存。

### 用户管理（admin）

```bash
# 创建用户（必须归属一个团队；返回的 token 只显示这一次）
TEAM_ID=$(curl -s -X POST http://127.0.0.1:8000/api/teams \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"ops"}' | python -c 'import sys,json;print(json.load(sys.stdin)["team"]["team_id"])')
curl -s -X POST http://127.0.0.1:8000/api/auth/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"username\":\"bob\",\"password\":\"secret123\",\"role\":\"user\",\"team_id\":\"$TEAM_ID\"}"

# 用户列表 / 轮换 token / 改密 / 删除
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/auth/users
# 轮换 token（可选 {"expires_in_days":30}，省略=永久）
curl -s -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/auth/users/bob/token
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"password":"newsecret456"}' http://127.0.0.1:8000/api/auth/users/bob/password
curl -s -X DELETE -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/auth/users/bob

# 自助轮换自己的 token（任意用户；可选有效期天数，留空=永久）
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"expires_in_days":90}' http://127.0.0.1:8000/api/auth/token
```

### 6. Web 看板

打开浏览器访问：

```
http://127.0.0.1:8000/
```

使用默认账号 `admin / admin` 登录（**登录后请在右上角用户菜单「个人中心」修改密码**）。节点页可查看/操作有权访问的节点、改别名与描述（**删除节点、升级探针仅 admin**）；任务页支持分页、按状态过滤、弹窗查看详情、一键终止任务，并显示任务来源用户；配置页可设公开地址和默认 LLM（保存后自动同步到所有在线节点），并查看服务端/探针版本、上传探针安装包；右上角用户菜单「个人中心」可查看账号信息、生成/轮换自己的 API token（可设有效期）并下载 agent-mesh 技能包；「用户」页（仅 admin 可见）可创建/删除用户、重置密码、轮换 API token。

## 生产部署

### 一键安装到 /opt/agent-mesh

```bash
cd <repo>
sudo ./deploy/install.sh
```

安装后会：
- 部署 orchestrator 到 `/opt/agent-mesh`（systemd 服务、开机自启），数据路径指向 `/opt/agent-mesh/data`
- 生成随机 token 保存到 `/opt/agent-mesh/etc/orchestrator.env`（**已存在则保留，不覆盖**）
- **构建并发布 edge 探针包到 `/opt/agent-mesh/data/bootstrap/`**（含 opencode），供节点安装与自动升级

> 只装服务端：`sudo ./deploy/install.sh --no-probe`；本机无 opencode：`--opencode /path/opencode`（否则自动从 GitHub 下载）。

### 服务管理

```bash
systemctl start agent-mesh-orchestrator
systemctl status agent-mesh-orchestrator

# 日志（服务输出走 systemd journal）
journalctl -u agent-mesh-orchestrator -f
```

> `deploy/install.sh` 负责 **orchestrator** 与**探针安装包的构建/发布**；edge 探针本身不经此部署，而是在各目标机器上用 bootstrap（`GET /api/bootstrap/install.sh`）安装到 `/opt/agent-mesh-agent`。

### 升级

```bash
./deploy/redeploy.sh
```

### 卸载

```bash
./deploy/uninstall.sh
```

## 远程 Agent 一键安装

在 orchestrator 上先构建/更新安装包（`install.sh` 已自动完成；手动构建见下）：

```bash
# 用部署好的 venv 构建，缺 opencode 会自动从 GitHub 下载
/opt/agent-mesh/lib/venv/bin/python scripts/build-agent-bootstrap.py \
    --output-dir /opt/agent-mesh/data/bootstrap
# 或用本机 opencode：--opencode /path/to/opencode
```

在 orchestrator Web 看板的 **配置** 页面设置 **Public URL**（例如 `http://10.0.0.1:8000`），然后节点页右上角 **「+ 安装新节点」** 生成安装命令，在目标机器上执行：

```bash
TOKEN='你的用户token' bash <(curl -fsSL -H "Authorization: Bearer $TOKEN" http://10.0.0.1:8000/api/bootstrap/install.sh)
```

> 该接口要求用户 token，故 curl 需带 `-H "Authorization: Bearer $TOKEN"`（看板「+ 安装新节点」生成的命令已自动带上）。

可选 `EDGE_ALIAS` 覆盖节点标识（`agent_id`）：`EDGE_ALIAS='别名' TOKEN='...' bash <(...)`。注意它设置的是节点标识、**不是** `agents.alias` 字段；改 DB 别名请在节点详情弹窗操作。

安装脚本会：
- 检测系统平台与架构
- 下载匹配的自包含安装包（PyInstaller 二进制 + opencode）
- 前置检查：root 权限、磁盘空间、已安装冲突（需 `FORCE_REINSTALL=1` 覆盖）
- 解压到 `/opt/agent-mesh-agent`
- 写入 `edge.env`
- 注册 systemd / launchd 服务并**自动启动**
- 启动后自动以 device_id（machine-id）注册节点

> 当前支持 Linux 与 macOS，Windows 后续支持。

## 删除节点

节点页点「删除」，orchestrator 会下发 `command` 卸载任务，让该节点：
1. 停/禁 systemd 服务
2. 删除 `/etc/systemd/system/agent-mesh-edge.service`
3. 删除 `/opt/agent-mesh-agent`
4. 随后删除数据库中的节点记录

## Agent 自升级与 LLM 配置同步

### 自升级

- **自动升级（默认开启）**：orchestrator 构建/更新了新版安装包后，版本低于最新包的节点会在空闲时自动升级，无需逐个操作。配置页可关闭（`auto_upgrade=0`）。
- **手动升级**：在 orchestrator 上先构建/更新安装包并发布到 `/opt/agent-mesh/data/bootstrap/`（`BUILD_PROBE=1 ./deploy/redeploy.sh`，或手动跑 `scripts/build-agent-bootstrap.py --output-dir /opt/agent-mesh/data/bootstrap`），然后节点页点「升级」或：

```bash
curl -s -X POST http://127.0.0.1:8000/api/agents/1/upgrade \
  -H "Authorization: Bearer $TOKEN"
```

节点在下一次心跳收到升级指令，**空闲时**（当前无执行中任务）自动：
1. 从 `/api/bootstrap/{os}-{arch}.tar.gz` 下载新版安装包（使用节点自己的 token）
2. staging 解压校验后，将当前 `bin/agent-mesh-edge.bin` 备份为 `.bin.old`，再原子替换
3. 写入带回滚逻辑的 wrapper 与 `etc/upgrading` 标记，通过 `systemctl restart`（Linux）/ `launchctl kickstart`（macOS）重启，失败回退 `execv`；systemd 未启用时走 `execv`
4. 新版本首次成功心跳后确认健康并清除标记与备份；若新二进制启动失败，wrapper 在下次启动时检测到未确认标记会自动回滚到旧版本（`.bin.old` + 恢复旧版本号）

> 升级请求会持续保留，直到节点上报的目标版本到达。节点上报的版本写入 `agents.version`，看板会展示当前/目标版本。

### LLM 配置同步

配置页「默认 LLM 配置」保存后（或 `PATCH /api/agents/{id}/llm_config` 设置节点级配置），`config_version` 自动自增，并随下一次心跳推送解析后的配置（节点级优先于全局）。边沿节点对比本地 `etc/config_version`，变更时即时更新运行中的 executor，并重写 `etc/edge.env` 使重启后仍然生效。

## 目录结构

```
agent-mesh/
├── src/agent_mesh/          # 源码
│   ├── orchestrator/        # 调度器
│   │   ├── api/             # REST 层（按领域拆分：auth/tasks/agents/edge/artifacts/bootstrap/files/skills）
│   │   ├── store/           # 存储层（connection/ 统一对接层 + sqlite/ pg/ / migrations）
│   │   └── web_ui/          # 看板（index.html / style.css / js/*）
│   ├── edge/                # 边沿 Agent
│   │   └── execution/       # 任务执行（common/command/llm）
│   └── shared/              # 公共模型/常量
├── scripts/                 # 开发启动脚本 / 打包脚本
├── deploy/                  # 生产部署脚本
├── skills/                  # OpenCode skill 文档
├── tests/                   # 测试
├── pyproject.toml
├── docs/                      # 设计文档（docs/README.md 索引）
```

## 配置

### orchestrator 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGENT_MESH_HOST` | `0.0.0.0` | 监听地址 |
| `AGENT_MESH_PORT` | `8000` | REST/Web 端口 |
| `AGENT_MESH_TOKEN` | 空（生产必设） | 全局 Edge token；空值禁用 global 身份 |
| `AGENT_MESH_PUBLIC_URL` | - | 对外 URL（用于 Agent 安装脚本） |
| `AGENT_MESH_WORKERS` | `cpu 核数` | 预留；当前不生效（实际单进程） |
| `AGENT_MESH_DB_TYPE` | `sqlite` | 存储后端：`sqlite` / `pg` |
| `AGENT_MESH_DB_PATH` | `./data/agent-mesh.db` | SQLite 路径 |
| `AGENT_MESH_PG_DSN` | - | PostgreSQL DSN（db_type=pg 时） |
| `AGENT_MESH_PG_HOST` | `127.0.0.1` | PG 主机 |
| `AGENT_MESH_PG_PORT` | `5432` | PG 端口 |
| `AGENT_MESH_PG_USER` | `agent_mesh` | PG 用户 |
| `AGENT_MESH_PG_PASSWORD` | - | PG 密码 |
| `AGENT_MESH_PG_DATABASE` | `agent_mesh` | PG 数据库名 |
| `AGENT_MESH_SWEEP_INTERVAL_S` | `5` | 扫描间隔 |
| `AGENT_MESH_OFFLINE_AFTER_S` | `15` | 离线判定时间（`deploy/install.sh` 生成的 `orchestrator.env` 会覆盖为 `30`） |
| `AGENT_MESH_SESSION_TTL_S` | `86400` | 登录 session token 有效期（秒） |
| `AGENT_MESH_ARTIFACT_DIR` | `./data/artifacts` | 产物存储目录 |
| `AGENT_MESH_ARTIFACT_MAX_SIZE_MB` | `50` | 单产物大小上限 |
| `AGENT_MESH_ARTIFACT_MAX_TOTAL_MB` | `200` | 产物总上限 |

> `max_concurrent`（每节点并发任务数，默认 2）不是环境变量，而是运行时可调设置（配置页或 `PATCH /api/settings`），随心跳下发到在线节点。

### edge agent 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EDGE_AGENT_ID` | hostname | 边沿显示名 |
| `EDGE_ORCHESTRATOR_URL` | `http://127.0.0.1:8000` | orchestrator 地址 |
| `EDGE_TOKEN` | 空（必设） | 认证 token（全局 token / 独立 agent token） |
| `EDGE_HEARTBEAT_S` | `3` | 心跳间隔 |
| `EDGE_RUNTIME` | `opencode` | CLI 运行时 |
| `EDGE_WORKDIR` | `.` | 默认工作目录（**任务未指定 workdir 时的兜底**；显式指定仍以任务为准）。包内 `install.sh` 会写入 `${INSTALL_DIR}/work` |
| `EDGE_INSTALL_DIR` | `/opt/agent-mesh-agent` | 安装目录（machine-id 持久化位置） |
| `EDGE_LLM_API_KEY` | - | LLM API key |
| `EDGE_LLM_BASE_URL` | - | LLM base URL |
| `EDGE_LLM_MODEL` | 空（**必配**） | 默认模型（网关真实完整 id；空且任务未传 `model` 时 llm 任务被拒绝） |
| `EDGE_LLM_MODELS` | 空 | 可用模型列表（逗号/换行；由 config-sync 下发，一般无需手填） |
| `EDGE_SYSTEM_PROMPT` | 空 | 节点级 system prompt（由 config-sync 下发，一般无需手填） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

> 节点身份唯一性由 device_id（/etc/machine-id 或安装目录持久化的随机 id）决定，`EDGE_AGENT_ID` 仅作显示名。

## 主要接口

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 账号密码登录，返回**短期 session token**（`token` 字段） |
| GET | `/api/auth/me` | 当前用户信息（含 `token_created_at`/`token_expires_at`） |
| POST | `/api/auth/token` | 自助轮换**自己**的 API token（可选 `{"expires_in_days":N}`，留空=永久；明文返回一次） |
| POST | `/api/auth/users` | **admin**：创建用户，返回一次性 API token |
| GET | `/api/auth/users` | **admin**：用户列表 |
| DELETE | `/api/auth/users/{username}` | **admin**：删除用户 |
| POST | `/api/auth/users/{username}/token` | **admin**：轮换 API token（返回一次，可选 `expires_in_days`） |
| POST | `/api/auth/users/{username}/password` | **admin**：重置指定用户密码 |
| POST | `/api/auth/change-password` | 修改自己的密码 |

### REST API（主 Agent / 脚本）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks/dispatch` | 下发任务（`mode` 必填；`session_id` 选填续用 opencode 会话；`attachments` 选填引用文件库 file_id） |
| GET | `/api/tasks/{task_id}` | 查询任务详情 |
| GET | `/api/tasks/{task_id}/status` | 查询任务状态 |
| POST | `/api/tasks/{task_id}/cancel` | 终止任务 |
| GET | `/api/tasks/{task_id}/logs` | 任务实时执行输出（增量，`?after_id=`、`?limit=`） |
| GET | `/api/tasks` | 任务列表（支持 `limit`/`offset` 分页、`status`/`mode` 过滤、`search` 模糊、`started_after`/`started_before` 开始时间区间，返回 `total` 总数） |
| DELETE | `/api/tasks/{task_id}` | 删除单个任务 |
| POST | `/api/tasks/batch-delete` | 批量删除：`{"task_ids": [...]}` 按 id；或 `{"all_matching": true, "status": "..."}` 删除匹配筛选的全部任务 |
| GET | `/api/agents` | 节点列表 |
| GET | `/api/agents/{id}` | 节点状态 |
| GET | `/api/agents/{id}/detail` | 节点详情（含最近任务） |
| PATCH | `/api/agents/{id}/alias` | 设置节点别名 |
| PATCH | `/api/agents/{id}/description` | 设置节点描述（主 Agent 识别用途；不影响执行） |
| PATCH | `/api/agents/{id}/system_prompt` | 设置节点级 system prompt（**admin**；随心跳同步到该节点） |
| PATCH | `/api/agents/{id}/llm_config` | 设置节点级 LLM 配置（**admin**；随心跳同步到该节点） |
| POST | `/api/agents/{id}/upgrade` | **admin**：请求升级节点（空闲时自动执行并回滚） |
| DELETE | `/api/agents/{id}` | **admin**：删除节点（下发卸载任务） |
| GET | `/api/settings` | 全局配置（**admin**） |
| PATCH | `/api/settings` | 更新全局配置（**admin**） |
| POST | `/api/artifacts/{task_id}` | 上传产物 |
| GET | `/api/artifacts/{task_id}` | 列出产物 |
| GET | `/api/artifacts/{task_id}/{artifact_id}` | 下载产物 |
| GET | `/api/bootstrap/install.sh` | 新节点安装脚本 |
| GET | `/api/bootstrap/info` | **admin**：服务端版本 + 已发布探针版本（配置页展示） |
| POST | `/api/bootstrap` | **admin**：上传/替换探针安装包（`.tar.gz`） |
| GET | `/api/bootstrap/{filename}` | 安装包下载 |
| GET | `/api/skills` | 技能摘要列表（**仅 name/description/version/enabled，不含内容**；边沿/用户 token 均可） |
| GET | `/api/skills/{name}` | 单个技能摘要 |
| POST | `/api/skills` | 上传技能 zip（提取并校验 SKILL.md，重复上传 version+1） |
| PATCH | `/api/skills/{name}` | 启停用技能 |
| DELETE | `/api/skills/{name}` | 删除技能 |
| GET | `/api/skills/{name}/download` | 下载技能 zip（agent 用自己的 token 按需拉取） |
| POST | `/api/files` | 上传文件到文件库（multipart `files`，返回 `file_id` + `md5`，同内容去重） |
| GET | `/api/files` | 文件库列表（`search` 可选按文件名/文件ID模糊） |
| GET | `/api/files/{file_id}` | 下载文件库文件（任意 token，带 `X-File-Md5`） |
| POST | `/api/files/batch-delete` | 批量删除文件库文件 |
| POST | `/api/files/batch-download` | 批量下载文件库文件为 ZIP |
| DELETE | `/api/files/{file_id}` | 删除文件库文件（不校验引用） |
| GET | `/api/skill-pack/agent-mesh` | 下载个性化 agent-mesh 技能包 zip（SKILL.md + references + 生成的 `.env`，已填充公开地址；token 放在 `.env`，不写入 Markdown） |

> 模板管理（`/api/templates*`、`/api/agents/{id}/template`）与团队管理（`/api/teams*`）**仅限 Web 管理平台**（需 `X-Agent-Mesh-UI` 头，外部调用一律 404），不在主 Agent/脚本可用接口内。

### REST API（边沿 Agent）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/edge/poll_for_task` | 心跳 + 领任务 + 上报系统/指标（字段规范见 [docs/standards/edge-reporting.md](docs/standards/edge-reporting.md)） |
| POST | `/api/edge/mark_started` | 开始执行通知 |
| POST | `/api/edge/submit_result` | 提交结果 |
| POST | `/api/edge/get_task_status` | 查询任务状态 |
| POST | `/api/edge/task_log` | 上报任务实时执行输出（llm/command 增量流） |

## 技能库

管理端在 Web 看板「技能」页上传技能 zip（zip 内需含带 frontmatter 的 `SKILL.md`，`name` 需匹配 `^[a-z0-9]+(-[a-z0-9]+)*$` 且 `description` 必填），或通过 REST：

```bash
# 上传
curl -s -X POST http://127.0.0.1:8000/api/skills \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@my-skill.zip"

# 浏览摘要（不含内容）
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/skills

# 按需下载完整技能（边沿 agent 用自己的 token 调取）
curl -s -H "Authorization: Bearer <agent-token>" \
  http://127.0.0.1:8000/api/skills/my-skill/download -o my-skill.zip
```

边沿执行 llm 任务时，提示词中会注入技能库使用指引（引用 `$EDGE_TOKEN`/`$ORCHESTRATOR_URL` 环境变量，token 不会明文进入提示词），由 opencode agent **自主决定**是否浏览摘要、下载并解压 `SKILL.md` 使用。技能内容仅通过下载接口按需下发，摘要接口永不暴露正文。

## 文件库（任务附件）

需要给任务附带文件时，先上传到文件库拿到 `file_id`，派发时通过 `attachments` 引用；探针执行前会把附件下载到任务工作目录并按 md5 校验，**下载失败或 md5 不符会标记任务失败**（`summary: attachment download failed`）：

```bash
# 1. 上传（可多文件；同 md5+文件名 自动去重返回原 file_id，非 admin 去重范围限于自己创建的文件）
curl -s -X POST http://127.0.0.1:8000/api/files \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@./myfile.txt" -F "files=@./config.yaml"
# => {"files":[{"file_id":"f-xxxx","filename":"myfile.txt","size":123,"content_type":"text/plain","md5":"...","download_url":"/api/files/f-xxxx"}]}

# 2. 派发任务时带上 attachments
curl -s -X POST http://127.0.0.1:8000/api/tasks/dispatch \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":1,"mode":"llm","instruction":"读取并分析工作目录下的 myfile.txt","attachments":["f-xxxx"]}'
```

- 附件以原文件名写入工作目录；llm 模式提示词会提示"工作目录可能已放入附件"。
- 文件库独立于任务，可在 Web 看板「文件」页管理（上传/下载/删除）；删除不校验引用，被删文件的任务执行时会因下载失败而标记失败。
- 最新版 agent-mesh 使用指南可从看板右上角用户菜单「个人中心」（或 `GET /api/skill-pack/agent-mesh`）下载为 `agent-mesh.zip`：含 SKILL.md 索引与 `references/`（示例中的公开地址已自动填充），以及写有你用户 token 的 `.env`。解压后放进主 Agent 的 skills 目录即可。

## 测试

```bash
uv run --no-sync python -m pytest tests/ -q
```

## 安全提示

- 生产环境务必修改默认 `admin` 密码（`POST /api/auth/change-password`），并通过用户管理接口创建专用账号
- 用户 API token 只在创建/轮换时返回一次，丢失后用户可在「个人中心」自助轮换（`POST /api/auth/token`），或由 admin 轮换（`POST /api/auth/users/{username}/token`）
- API token 以 SHA-256 哈希存库；可选有效期 `token_expires_at`（`NULL`=永久，迁移 019），过期后鉴权拒绝；登录返回的 session token 默认 24h 过期
- 使用 HTTPS 网关终止 TLS
- 边沿 Agent 使用低权限账号运行
- LLM API key 存于 orchestrator `settings`（DB，配置页写入）并经心跳下发到节点 `etc/edge.env`；安装脚本不内嵌任何凭据，`edge.env`/`orchestrator.env` 建议 600 权限
- REST API 使用用户 token 认证；Edge 协议使用用户 token / 全局 `AGENT_MESH_TOKEN` / agent 独立 token
- 删除节点会卸载目标机器上的 agent，谨慎操作

## 文档

- `docs/README.md` — 设计文档索引（架构 / 协议 / 任务执行 / 认证 / 部署 / 已知问题）
- `docs/standards/edge-reporting.md` — **上报接口标准**：边沿 Agent 上报字段规范与治理流程
- `skills/agent-mesh/SKILL.md` — OpenCode skill（主 Agent 操作指南）
- `deploy/README.md` — 部署指南

## License

MIT
