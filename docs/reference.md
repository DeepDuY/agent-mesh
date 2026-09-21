# 参考手册

> 环境变量、REST 接口与使用示例。快速部署见根目录 [README.md](../README.md)，生产部署见 [deployment.md](./deployment.md)。

## 目录

- [本地开发快速开始](#本地开发快速开始)
- [环境变量](#环境变量)
- [主要接口](#主要接口)
- [技能库](#技能库)
- [文件库（任务附件）](#文件库任务附件)
- [自升级与 LLM 配置同步](#自升级与-llm-配置同步)
- [测试](#测试)

## 本地开发快速开始

### 环境要求

- **Python 3.12+**（部署脚本会校验；若该 Python 缺 `ensurepip`，脚本自动用 `get-pip.py` 兜底）
- 部署为 systemd 服务需 **root**；依赖 `rsync`、`tar`、`systemctl`
- 构建探针包需联网（pip 源；本机无 opencode 时会从 `github.com/sst/opencode` 下载）
- **目标边沿机器无需安装 opencode**（已打包进探针）
- 推荐用 `uv` 管理开发依赖；下方 REST 示例用 `jq` 解析 JSON（可选）

### 安装与启动

```bash
cd <repo>
export PATH="$HOME/.local/bin:$PATH"
uv sync

# 启动 orchestrator（默认 http://0.0.0.0:8000）
./scripts/start-orchestrator.sh

# 启动 edge agent（本机联调）
EDGE_AGENT_ID=client EDGE_ORCHESTRATOR_URL=http://127.0.0.1:8000 \
  uv run --no-sync python -m agent_mesh.edge.agent
```

> 当前只支持**单进程**运行：`AGENT_MESH_WORKERS>1` 会被忽略并打警告，实际仍按 1 个 worker 运行（多 worker 待设计，见 [known-issues.md](./known-issues.md) §1）。
> 使用 PostgreSQL：`AGENT_MESH_DB_TYPE=pg AGENT_MESH_PG_DSN='postgresql://user:pass@host/db'`（统一连接层 `store/connection/` 按进程惰性建 asyncpg 池）。
> ⚠️ 一律用 `uv run --no-sync`：直接 `uv run` 会重新解析依赖并尝试源码编译 `asyncpg`，在旧 glibc（<2.28）上会失败。

edge 的 LLM 配置通过 orchestrator 配置页/`PATCH /api/settings` 下发（心跳 config-sync，落盘 `edge.env`）；也可用 `EDGE_LLM_*` 环境变量预置。密钥不写进代码。

### 下发任务（REST）

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

### Web 看板

打开 `http://127.0.0.1:8000/`，使用默认账号 `admin / admin` 登录（**登录后请在右上角用户菜单「个人中心」修改密码**）。节点页可查看/操作有权访问的节点、改别名与描述（**删除节点、升级探针仅 admin**）；任务页支持分页、按状态过滤、弹窗查看详情、一键终止任务，并显示任务来源用户；配置页可设公开地址和默认 LLM（保存后自动同步到所有在线节点），并查看服务端/探针版本、上传探针安装包；右上角用户菜单「个人中心」可查看账号信息、生成/轮换自己的 API token（可设有效期）并下载 agent-mesh 技能包；「用户」页（仅 admin 可见）可创建/删除用户、重置密码、轮换 API token。

## 环境变量

### orchestrator

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGENT_MESH_HOST` | `0.0.0.0` | 监听地址 |
| `AGENT_MESH_PORT` | `8000` | REST/Web 端口 |
| `AGENT_MESH_TOKEN` | 空（生产必设） | 全局 Edge token；空值禁用 global 身份 |
| `AGENT_MESH_PUBLIC_URL` | - | 对外 URL（用于 Agent 安装脚本） |
| `AGENT_MESH_WORKERS` | `1` | 仅支持 `1`；`>1` 被忽略并打警告，始终单进程运行 |
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
| `AGENT_MESH_ARTIFACT_MAX_SIZE_MB` | `50` | 单产物大小上限（**已弃用**，仅作 ArtifactStore 兜底；改用配置页设置） |
| `AGENT_MESH_ARTIFACT_MAX_TOTAL_MB` | `200` | 产物总上限（**已弃用**，仅作 ArtifactStore 兜底；改用配置页设置） |

> **上传大小限制是运行时可调设置**（不是环境变量）：文件库单文件 `file_max_size_mb`、产物单文件 `artifact_max_size_mb`、单任务产物总量 `artifact_task_total_mb`、产物全库总量 `artifact_total_mb`、超出全库时回收最旧 `artifact_evict_oldest`、产物传输超时 `artifact_timeout_s`。在「配置 → 上传大小限制」或 `PATCH /api/settings` 调整；默认 100/100/100/200/开/300s。其中 `artifact_timeout_s` 随心跳下发给探针（探针上传用），页面下载产物也按此超时。
>
> `max_concurrent`（每节点并发任务数，默认 2）同样不是环境变量，而是运行时可调设置（配置页或 `PATCH /api/settings`），随心跳下发到在线节点。
>
> `schedule_timezone`（定时任务 cron 求值所用时区，IANA 名，默认空 = 系统时区）也是运行时可调设置；定时任务可在自身 `timezone` 字段覆盖。

### edge agent

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
| GET | `/api/settings` | 全局配置（**admin**，仅限 Web UI 头部） |
| PATCH | `/api/settings` | 更新全局配置（**admin**，仅限 Web UI 头部） |
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
| POST | `/api/edge/poll_for_task` | 心跳 + 领任务 + 上报系统/指标（字段规范见 [standards/edge-reporting.md](./standards/edge-reporting.md)） |
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

## 定时任务（cron 调度）

服务端**进程内调度器**按 cron 表达式周期派发任务（单进程；错过不补跑；上一轮任务未结束则跳过本轮）。

- cron 为 5 段（分 时 日 月 周），支持 `*`、`a`、`a-b`、`a,b`、`*/n`；周字段 0/7 均为周日；日/周同时限定时按 Vixie 语义取「或」。
- 求值时区：任务自身 `timezone` > 设置 `schedule_timezone` > 系统时区；`next_run_at` 以 UTC 存储。非法 cron 会停用该任务并记录原因。
- 可见性同任务：自己 + 团队，admin 全部。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/schedules` | 新建（`name`/`cron`/`agent_id`/`mode`/`instruction`；可选 `timezone`/`timeout_s`/`model`/`enabled`/`attachments`） |
| GET | `/api/schedules` | 列表（owner 可见） |
| GET | `/api/schedules/{id}` | 详情 |
| PATCH | `/api/schedules/{id}` | 更新（改 cron/时区/启停会重算下次运行） |
| DELETE | `/api/schedules/{id}` | 删除 |
| POST | `/api/schedules/{id}/run` | 立即运行一次（不影响下次运行） |

```bash
curl -s -X POST http://127.0.0.1:8000/api/schedules \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"nightly","cron":"0 3 * * *","agent_id":1,"mode":"command","instruction":"echo hi"}'
```

## 自升级与 LLM 配置同步

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
3. 写入带回滚逻辑的 wrapper 与 `etc/upgrading` 标记，通过 `systemctl restart`（Linux）/`launchctl kickstart`（macOS）重启，失败回退 `execv`；systemd 未启用时走 `execv`
4. 新版本首次成功心跳后确认健康并清除标记与备份；若新二进制启动失败，wrapper 在下次启动时检测到未确认标记会自动回滚到旧版本（`.bin.old` + 恢复旧版本号）

> 升级请求会持续保留，直到节点上报的目标版本到达。节点上报的版本写入 `agents.version`，看板会展示当前/目标版本。

### LLM 配置同步

配置页「默认 LLM 配置」保存后（或 `PATCH /api/agents/{id}/llm_config` 设置节点级配置），`config_version` 自动自增，并随下一次心跳推送解析后的配置（节点级优先于全局）。边沿节点对比本地 `etc/config_version`，变更时即时更新运行中的 executor，并重写 `etc/edge.env` 使重启后仍然生效。

## 测试

```bash
uv run --no-sync python -m pytest tests/ -q
```
