---
name: agent-mesh
description: Control the agent-mesh orchestrator via REST/MCP to dispatch tasks to remote edge agents, monitor live logs, cancel tasks, check node status/CPU/memory, manage node descriptions/templates, install or remove an edge agent, upgrade agents, sync LLM config/models, and use the skill library / file library for attachments. Use when the user wants to run work on another machine or inspect/manage the remote agent fleet.
---

# agent-mesh 编排器控制

通过编排器 REST API（或 MCP SSE）把任务委派给边缘节点（edge agent）、监控执行并管理节点与资源。

## 何时使用

- 用户想在远程边缘节点上运行命令、脚本、测试或自然语言任务。
- 用户想查看有哪些节点在线、状态、CPU/内存，或给节点改名/升级/删除/安装。
- 用户想查看/筛选之前派发的任务，实时看输出，或**终止**正在跑的任务。
- 用户想给 llm 任务附文件（文件库）或让边沿按需使用技能库。
- 用户想给某节点单独配置 LLM（节点级覆盖全局）。

## Base URL 与认证

- **REST Base URL**: `http://<orchestrator-host>:8000/api`
- **MCP SSE**: `http://<orchestrator-host>:8001/`
- **认证**: 先登录拿 session token，之后所有请求带 `Authorization: Bearer <token>`；MCP 同样要求用户 token

登录获取 session token（默认 24h 有效）：

```bash
curl -X POST http://<host>:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}'
# => {"username":"admin","token":"<session-token>","role":"admin","token_type":"session"}
```

之后的请求都带：

```
Authorization: Bearer <token>
```

长期使用的 API token 在创建用户时一次性下发（`POST /api/auth/users`，admin），或由 admin 通过 `POST /api/auth/users/{username}/token` 轮换获取；API token 以 SHA-256 哈希入库。注意：全局 token（`AGENT_MESH_TOKEN`）只用于 edge 节点的心跳/上报端点，REST 控制接口与 MCP 必须用**用户 token**。

## 节点标识（重要）

一个节点由**数字 id**（自增主键）唯一标识，同时上报 **device_id**（`/etc/machine-id`，或安装目录持久化的随机 id）。`agent_id` 是显示名/别名，不唯一。

引用节点时可传以下任一：
- 数字 id，如 `3`
- device_id（machine-id），如 `a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e`
- 显示名 `agent_id`

## 核心端点（主 Agent 常用）

| Method | Path | 用途 |
|--------|------|------|
| GET | `/healthz` | 健康检查 |
| POST | `/auth/login` | 登录拿 token |
| GET | `/agents` | 列出所有节点（在线/离线、版本、CPU/内存） |
| GET | `/agents/{id}` | 单个节点 |
| GET | `/agents/{id}/detail` | 节点详情 + 最近任务 |
| PATCH | `/agents/{id}/alias` | 设置/清除别名 |
| PATCH | `/agents/{id}/description` | 设置/清除节点描述（供主 Agent 识别节点用途） |
| PATCH | `/agents/{id}/system_prompt` | 设置**节点级** system prompt（与模板拼接，心跳同步） |
| PATCH | `/agents/{id}/template` | 绑定/解绑节点模板（`template_id`，null 解绑） |
| PATCH | `/agents/{id}/llm_config` | 设置**节点级** LLM 配置（覆盖全局，心跳同步） |
| GET | `/templates` | 模板列表 |
| POST | `/templates` | 新建模板（name/system_prompt/llm_model/allowed_tools/data） |
| GET | `/templates/{id}` | 模板详情 |
| PATCH | `/templates/{id}` | 更新模板（改动自动同步到所有绑定节点） |
| DELETE | `/templates/{id}` | 删除模板（绑定节点自动解绑） |
| POST | `/agents/{id}/upgrade` | 请求升级该节点（空闲时自动执行+回滚） |
| DELETE | `/agents/{id}` | 删除节点（下发卸载任务） |
| POST | `/tasks/dispatch` | 派发任务（command/llm；可带 `session_id`/`attachments`） |
| GET | `/tasks` | 任务列表（分页 + 状态/模式/搜索/开始时间筛选） |
| GET | `/tasks/{task_id}` | 任务详情 |
| GET | `/tasks/{task_id}/status` | 查询任务状态 |
| GET | `/tasks/{task_id}/logs` | 任务实时执行输出（增量 `?after_id=`） |
| POST | `/tasks/{task_id}/cancel` | 终止任务（edge 会杀掉整棵进程树） |
| DELETE | `/tasks/{task_id}` | 删除单个任务 |
| POST | `/tasks/batch-delete` | 批量删除（按 id 或按筛选条件） |
| POST | `/files` | 上传文件到文件库（multipart，返回 `file_id`+`md5`，同内容去重） |
| GET | `/files` | 文件库列表（可 `?search=`） |
| GET | `/files/{file_id}` | 下载文件库文件 |
| POST | `/files/batch-delete` | 批量删除文件库文件 |
| POST | `/files/batch-download` | 批量打包下载（ZIP） |
| GET | `/skills` | 技能库摘要（name/description/version/enabled） |
| GET | `/skills/{name}/download` | 下载技能 zip（给边沿按需取用） |
| GET | `/skill-doc/agent-mesh` | 下载**个性化**的 agent-mesh SKILL.md（已填地址+你的 token） |
| GET | `/settings` | 全局配置 |
| PATCH | `/settings` | 更新全局配置（LLM/公开地址/自动升级/并发数） |
| GET | `/bootstrap/install.sh` | 获取新节点安装命令脚本 |
| POST | `/auth/users`（admin） | 创建用户，返回一次性 API token |
| GET | `/auth/users`（admin） | 用户列表 |
| POST | `/auth/users/{username}/token`（admin） | 轮换用户 API token |
| POST | `/auth/change-password` | 修改自己的密码 |

## 派发任务

`POST /tasks/dispatch` 请求体：

```json
{
  "agent_id": 3,
  "mode": "command",
  "instruction": "echo hello && uname -a",
  "workdir": "/tmp",
  "timeout_s": 120,
  "max_retries": 0
}
```

- `agent_id`: 必填，节点的数字 id / device_id / 显示名。
- `mode`: 必填。
  - `command`: `instruction` 作为 shell 命令原样执行（`bash -c`），**不经过 LLM**。结果会**自动收集工作目录下新增文件**为产物。
  - `llm`: 交给节点本机的 opencode 运行时，`instruction` 是自然语言任务；只收集 LLM 声明的 artifacts。
- 可选：`workdir`、`timeout_s`（默认 300）、`model`（per-task 覆盖，须为网关真实完整 id 且命中 `list_models`；**留空用节点/模板/全局默认模型，没有默认模型时 llm 任务会被拒绝**）、`allowed_tools`、`output_limit`（默认 200000）、`max_retries`、`depends_on`、`session_id`、`skills`、`attachments`（文件库 `file_id` 列表）。
- 未显式 `workdir` 的任务自动落在独立子目录 `<EDGE_WORKDIR>/tasks/<task_id>/`，互不干扰。

**复用 LLM 会话（选填）**：若新任务要续用上一个 llm 任务的会话，先读该任务 `result.session_id`，派发时带上 `session_id`（节点 opencode 会 `--session <id>` 续跑）。任务完成后的 `result.session_id` 为本次实际使用的会话 ID。

响应：`{"task_id": "t-xxxxx", "status": "queued"}`，然后轮询状态直到终态。

## 任务状态与轮询

状态机：`queued → assigned → working → completed / failed / timed_out`，另有 `cancelled`（人为终止）。

轮询 `GET /tasks/{task_id}/status`：

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx/status
```

终态：`completed` / `failed` / `timed_out` / `cancelled`。结束后读 `task.result`。

## 取消任务（重要）

`POST /tasks/{task_id}/cancel`：

- 排队中（`queued`）：直接从队列移除，任务置 `cancelled`，不会执行。
- 已分配/执行中（`assigned`/`working`）：任务置 `cancelled`，**edge 在下次轮询（约 3s 内）检测到后终止整个执行进程组**（SIGTERM→SIGKILL，含 `bash -c` 派生的后台子进程与 opencode 的工具子进程），并**不提交结果**。
- 已是终态（completed/failed/timed_out/cancelled）：返回 `{"accepted": false}`（幂等）。

```bash
curl -s -X POST -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx/cancel
# => {"accepted": true, "task_id": "t-xxxxx", "status": "cancelled"}
```

## 任务列表 / 筛选 / 批量删除

`GET /tasks` 支持分页与筛选，返回 `{"tasks": [...], "total": N, "limit": 20, "offset": 0}`：

```bash
# 只看某状态，模糊搜指令，按开始时间区间
curl -s -H "Authorization: Bearer <token>" \
  "http://<host>:8000/api/tasks?status=working&mode=llm&search=hello&started_after=2026-08-01T00:00:00Z&limit=50&offset=0"
```

- `status`: queued/assigned/working/completed/failed/timed_out/cancelled；`mode`: command/llm。
- `search`: 指令/任务ID/节点模糊；`started_after`/`started_before`: ISO 时间，可组合出"之后/之间/之前"。

批量删除：

```bash
# 按 id
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"task_ids":["t-1","t-2"]}' http://<host>:8000/api/tasks/batch-delete

# 按筛选条件全删（如清空所有已终止任务）
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"all_matching":true,"status":"cancelled"}' http://<host>:8000/api/tasks/batch-delete
```

## 查看节点 / 管理节点

`GET /agents` 返回每个节点（含资源与版本信息）：

```json
{"agents": [{
  "id": 3, "device_id": "a1b2c3...", "agent_id": "node-3", "alias": null,
  "display_name": "node-3", "runtime": "opencode",
  "hostname": "host-3", "os": "linux", "distro": "centos 7", "arch": "x64",
  "version": "1.4.2", "online": true, "last_seen": "...",
  "current_task_id": null,
  "cpu_percent": 3.4, "mem_percent": 45.6, "mem_used_mb": 5405.5, "mem_total_mb": 11850.9,
  "llm_api_key": null, "llm_base_url": null, "llm_model": null,
  "description": "生产 Web 服务器", "system_prompt": null, "template_id": 2,
  "upgrade_requested": false, "upgrade_version": null
}]}
```

- `online`: 心跳期内是否在线；`version`: 探针当前版本；`display_name`: 别名 > 主机名 > agent_id。
- `description`: 节点用途说明（运维设置，主 Agent 选节点前先看）；`template_id`: 绑定的模板 id；`system_prompt`: 节点级提示词。
- 每节点默认最多同时执行 2 个任务（`max_concurrent`，配置页或 `PATCH /api/settings` 可调，随心跳下发）。派发到同一节点会在并发上限内并行执行。

节点级操作：

```bash
# 改别名（null 清除）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"alias":"生产节点A"}' http://<host>:8000/api/agents/3/alias

# 改节点描述（供主 Agent 识别用途；null 清除）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"description":"生产 Web 服务器，只跑部署类命令"}' http://<host>:8000/api/agents/3/description

# 节点级 system prompt（与绑定模板的提示词拼接，节点在前；null 清除）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"system_prompt":"你是运维专员，只操作 /opt 下的目录。"}' http://<host>:8000/api/agents/3/system_prompt

# 绑定/解绑模板
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"template_id":2}' http://<host>:8000/api/agents/3/template
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"template_id":null}' http://<host>:8000/api/agents/3/template

# 节点级 LLM 配置（覆盖全局；提交任意子集或 null）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"llm_model":"anthropic/deepseek-v4-flash","llm_base_url":"https://api.example.com/v1","llm_api_key":"sk-..."}' \
  http://<host>:8000/api/agents/3/llm_config

# 手动升级（空闲时自动下载新版并重启，失败自动回滚）
curl -s -X POST -H "Authorization: Bearer <token>" http://<host>:8000/api/agents/3/upgrade
# => {"requested": true, "version": "1.5.0", ...}
```

## 节点模板

模板是可复用的节点配置，节点**引用式绑定**（`agents.template_id`）。改模板会 `config_version` 自增并同步到所有绑定节点。字段：`system_prompt`（提示词）、`llm_model`（默认模型）、`allowed_tools`/`data`（预留，后续权限用）。

生效规则：
- 模型：`节点 llm_model > 模板 llm_model > 全局 settings.llm_model`（无默认兜底，都没有则 llm 任务被拒）。
- 提示词：`内置 wrapper + 节点 system_prompt + 模板 system_prompt`（节点在前）。

```bash
# 新建模板
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name":"ops","description":"部署类节点","system_prompt":"你是部署专员。","llm_model":"anthropic/deepseek-v4-flash"}' \
  http://<host>:8000/api/templates

# 列表 / 更新 / 删除
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/templates
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"system_prompt":"新提示词"}' http://<host>:8000/api/templates/2
curl -s -X DELETE -H "Authorization: Bearer <token>" http://<host>:8000/api/templates/2
```

- **可用模型**：`list_models`（MCP）/ 配置页「可用模型列表」（`settings.llm_models`）是模型唯一来源；派发 llm 任务时 `model` 必须命中该列表。
- **LLM 配置同步**：保存全局、节点级、或模板后 `config_version` 自增，随下次心跳推给节点，边沿自动更新运行中 executor 并持久化（模型/提示词）。节点级优先/叠加模板。
- **自升级**：默认开启自动升级（`auto_upgrade=1`），节点版本低于 `data/bootstrap/VERSION` 且空闲时自动升级；也可用上面接口手动触发。
- 删除节点会下发卸载任务，谨慎操作。

## 实时执行输出

边沿会把执行实时输出流式上报，可增量拉取观察 LLM 正在做什么 / command 的输出：

```bash
# 首次（after_id=0），limit 可选
curl -s -H "Authorization: Bearer <token>" "http://<host>:8000/api/tasks/t-xxxxx/logs?after_id=0"
# => {"task_id":"t-xxxxx","logs":[{"id":1,"kind":"text","content":"..."}],"next_id":2}

# 增量拉取
curl -s -H "Authorization: Bearer <token>" "http://<host>:8000/api/tasks/t-xxxxx/logs?after_id=2"
```

- `kind`: `text`（LLM 文本）/ `error` / `complete` / `raw`（command 原始行）。
- 任务结束仍可查；被删除则不可查。MCP 侧对应工具 `get_task_logs(task_id, after_id)`。

## 技能库

边沿执行 llm 任务时提示词内置技能库指引，由 opencode agent **自主**浏览摘要、按需下载使用（主 Agent 无需手动下发技能文件；如想让某任务用某技能，可在 `instruction` 中提示）。

```bash
# 浏览摘要（仅 name/description/version/enabled，无内容）
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/skills

# 给边沿按需下载
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/skills/<name>/download -o skill.zip
```

- 技能库**管理**：Web 看板「技能」页或 REST：`POST /api/skills`（上传 zip，需含带 name/description frontmatter 的 `SKILL.md`；重传同名 version+1）、`PATCH /api/skills/{name}`（启停用）、`DELETE /api/skills/{name}`、`GET /api/skills/{name}/download`。
- agent-mesh 自身的使用指南：`GET /api/skill-doc/agent-mesh` 下载**已填好公开地址与你的 token** 的个性化 SKILL.md，放入主 Agent 技能目录即可用（示例可直接复制执行）。

## 文件库（任务附件）

先上传到文件库拿 `file_id`，派发时用 `attachments` 引用；探针执行前下载到工作目录并按 md5 校验，失败则任务标记失败（`summary: attachment download failed`）。

```bash
# 1. 上传（多文件；同 md5+文件名 自动去重返回原 file_id）
curl -s -X POST http://<host>:8000/api/files \
  -H "Authorization: Bearer <token>" -F "files=@./myfile.txt" -F "files=@./config.yaml"
# => {"files":[{"file_id":"f-xxxx","filename":"myfile.txt","size":123,"content_type":"text/plain","md5":"...","download_url":"/api/files/f-xxxx"}]}

# 2. 派发时带上 attachments
curl -s -X POST http://<host>:8000/api/tasks/dispatch \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"llm","instruction":"读取并分析工作目录下的 myfile.txt","attachments":["f-xxxx"]}'
```

- 附件以原文件名写入任务工作目录；llm 模式提示词会提示"工作目录可能已有附件"。
- 列表/批量：`GET /api/files?search=`；`POST /api/files/batch-delete`（`{"file_ids":[...]}`）；`POST /api/files/batch-download`（`{"file_ids":[...]}`，返回 ZIP）。
- 删除不校验引用：被删文件的任务执行时因下载失败标记失败。

## 用户管理（admin）

```bash
# 创建用户（token 只显示这一次，妥善保存）
curl -s -X POST http://<host>:8000/api/auth/users \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"username":"bob","password":"secret123","role":"user"}'

# 用户列表 / 轮换 API token / 重置他人密码 / 删除
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/auth/users
curl -s -X POST -H "Authorization: Bearer <token>" http://<host>:8000/api/auth/users/bob/token
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"password":"newsecret456"}' http://<host>:8000/api/auth/users/bob/password
curl -s -X DELETE -H "Authorization: Bearer <token>" http://<host>:8000/api/auth/users/bob

# 改自己的密码
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"old_password":"...","new_password":"..."}' http://<host>:8000/api/auth/change-password
```

## 安装 / 删除节点

安装（新机器装 edge 探针，需**长期有效**的 token —— 全局 token 或用户 API token，不要用 24h 的 session token）：

```bash
TOKEN='<长期 token>' bash <(curl -fsSL -H "Authorization: Bearer $TOKEN" \
  http://<host>:8000/api/bootstrap/install.sh)
```

- 可选 `EDGE_ALIAS` 覆盖节点标识（`agent_id`，**非** DB 别名）；安装脚本自动注册 systemd/launchd 并启动，以 device_id 自动注册节点。
- 删除节点会下发卸载命令并移除记录（谨慎）。

## MCP 工具（SSE :8001，共 13 个）

| 工具 | 用途 |
|------|------|
| `list_agents` | 列出节点（含 `description` 用途说明，选节点前先看） |
| `get_agent` / `get_agent_detail` | 单节点 / 详情+最近任务 |
| `set_agent_alias` | 设置/清除别名 |
| `list_models` | 可用 LLM 模型 id（派发 llm 任务时从中选 `model`） |
| `dispatch_task` | 派发任务（command/llm；可带 `model`/`session_id`/`attachments`/`skills`） |
| `list_tasks` | 任务列表 |
| `get_task_status` | 任务状态 |
| `get_task_logs` | 实时执行输出（`task_id`、`after_id` 增量） |
| `cancel_task` | 终止任务 |
| `list_skills` | 技能库摘要 |
| `poll_for_task` / `submit_result` | 边沿心跳内部用，主 Agent 一般不用 |

MCP 通道**只接受用户 token**（登录 session token 或用户 API token），全局 `AGENT_MESH_TOKEN` 不适用。

## 完整流程示例

1. `GET /agents` → 找目标节点 id（在线、版本正常）。
2. `POST /tasks/dispatch`：`mode=command` 跑命令 / `mode=llm` 跑自然语言任务 → 拿 `task_id`。
3. 需要进度时 `GET /tasks/{task_id}/logs?after_id=<next_id>` 增量看实时输出。
4. 每 2-3 秒 `GET /tasks/{task_id}/status`，直到终态。
5. 用户要停就 `POST /tasks/{task_id}/cancel`（edge 会杀掉整棵进程组）。
6. 结束读 `task.result`（summary/exit_code/stdout_tail/artifacts/session_id）。

## 安全

- 保管好 token；生产用 HTTPS；MCP/REST 用用户 token，不把全局 token 用于控制面。
- LLM API key 仅存服务端 DB / 节点 `edge.env`，经心跳 config-sync 下发；不要写入代码或仓库。
- 删除节点会卸载目标机器的 agent，谨慎执行；取消任务会终止进程组。

## 示例脚本

见 `references/example-poll.py`。
