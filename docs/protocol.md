# 协议：进程间交互、节点标识与通信协议

> 来源：原 DESIGN.md §3 / §4 / §7。上报字段的完整规范见 [standards/edge-reporting.md](./standards/edge-reporting.md)。

## 1. 进程间交互总览

### 1.1 消息路径汇总

| 方向 | 通道 | 说明 |
|------|------|------|
| 主 Agent → orchestrator | REST :8000 | `/api/tasks/dispatch`、`/api/tasks/{id}`、`/api/tasks/{id}/cancel`、`/api/agents/*`、`/api/settings`、`/api/bootstrap/*` 等（需用户 token） |
| 边沿 Agent → orchestrator | REST :8000 | `/api/edge/poll_for_task`（心跳+领任务）、`mark_started`、`submit_result`、`get_task_status`（全局 token / agent 独立 token） |
| 边沿 Agent → orchestrator | REST :8000 | `POST /api/artifacts/{task_id}`（产物上传，multipart，任选 token） |
| 浏览器 → orchestrator | REST :8000 | 5 秒轮询 `GET /api/agents`、`/api/tasks`、`/api/settings`（用户 token） |
| orchestrator → 边沿 Agent | 被动 | 仅在边沿心跳时通过响应返回任务（无主动推送） |

### 1.2 一次任务生命周期（端到端时序）

```
主 Agent                 orchestrator                      边沿 Agent
   │ dispatch_task(agent, instr, mode, ...)                   │
   │──────────────────────▶│                                  │
   │                       │ resolve_agent → queue_key        │
   │                       │ create Task(queued, agent_id=key) │
   │                       │ enqueue(task_id, queue_key)       │
   │                       │◀────────────────────── 心跳 poll_for_task
   │                       │  upsert_agent(online, last_seen) │
   │                       │  dequeue(key) → task_id          │
   │                       │  status: queued → assigned        │
   │                       │  set_agent_current_task(key, id)  │
   │                       │ 返回任务 ────────────────────────▶│
   │                       │◀────────────── mark_started ──────│
   │                       │  status: assigned → working       │
   │                       │  started_at = now                 │
   │                       │                                   │ mode=command → bash -c
   │                       │                                   │ mode=llm → opencode run
   │                       │◀── POST /api/artifacts/{task_id}──│ 上传产物文件
   │                       │◀────── submit_result ─────────────│
   │                       │  set_task_result                  │
   │                       │  status → completed / failed      │
   │                       │  clear current_task               │
   │ get_task_status(id)───▶│                                   │
   │◀──────────────────────│ 返回 Task + result                 │
```

### 1.3 orchestrator 启动时序

```
main()
  ├─ config = OrchestratorConfig()
  ├─ 选择后端: db_type=="pg" → PostgresStore(...) 否则 SQLiteStore(db_path)
  ├─ store = TaskStore(store_backend, sweep_interval_s, offline_after_s)
  ├─ asyncio.run(store_backend.initialize())   # 跑迁移 + 确保 admin
  └─ _run_single_process():                    # 单进程
       └─ store.start_sweepers() → uvicorn.run(app, :8000)
```

> 说明：`AGENT_MESH_WORKERS>1` 目前不生效（`uvicorn.Server.serve()` 忽略 `workers`），实际始终单进程；多 worker 待修复，见 [architecture.md §1.3](./architecture.md#13-运行模式单进程)。

### 1.4 边沿 Agent 启动与心跳循环时序

```
EdgeAgent.run()
 ├─ 注册 SIGTERM/SIGINT → _stop_event
 ├─ device_id = get_device_id(install_dir)
 └─ loop（每 heartbeat_s≈3s 一次）：
     ├─ poll_for_task(agent_id, device_id, runtime, hostname, os, distro, arch, version, metrics)
     ├─ 若返回 task → _execute(task)：mark_started → 执行 → 传产物 → submit_result
     ├─ 出错 → 指数退避 1s→30s 上限后重试
     └─ 收到 stop 信号退出
```

### 1.5 心跳领取与断线重连

`task_store.py:heartbeat()` 是边沿与 orchestrator 的核心交互点：

1. `key = device_id or agent_id`（稳定键，后续队列、current_task 都用它）。
2. 先读旧记录保留 `current_task_id`，再 `upsert_agent(online=True, last_seen=now)`。
3. **多任务并发分配（v1.4.0）**：请求带 `running_tasks`（边沿当前正在执行的任务 id 列表）；orchestrator 以 `tasks` 表 assigned/working 状态统计活跃数，`resume = 活跃但不在 running_tasks 的`（边沿崩溃重启后继续执行，不重复派发），`capacity = max_concurrent − 活跃数`。
4. 循环 `dequeue(key)` 补足 capacity：SQLite 单语句 `DELETE ... RETURNING`（原子，跨 worker 安全），PG 同语义。
5. 领到且状态为 `queued` → 置 `assigned`、写 `assigned_at`、`set_agent_current_task(key, task_id)`，加入返回集。
6. 响应 `{tasks: [...], task: tasks[0]?, server_ts, max_concurrent, config_version, config, agent_token?, upgrade?}`（`task` 为兼容旧版探针的便捷视图）。

## 2. 节点标识

- 一个节点由 **数字自增主键 `id`** 唯一标识。
- 同时上报 **`device_id`**（`/etc/machine-id`，或安装目录持久化的随机 id）作为设备级稳定标识，也是**队列键**。
- `agent_id`：显示名/别名，**不唯一**。
- 派发/查询时 `agent_id` 参数可传：**数字 id → device_id → 显示名** 三级，由 `TaskStore._resolve_agent()` 统一解析（同名显示名取 `last_seen` 最新的）。
- 注册：边沿以 device_id 为键 upsert，同 device_id 重复上报只更新不新增。

## 3. 通信协议

### 3.1 控制面：主 Agent → orchestrator（REST）

> ⚠️ 原 MCP 通道（SSE :8001）已整体移除：`mcp_server.py`、`mcp` 依赖、SSE 服务与 13 个 MCP 工具均不存在。主 Agent 一律通过 §3.3 的 REST 端点完成派发与查询；`agent_id` 统一接受 数字 id / device_id / 显示名。

### 3.2 边沿 Agent → orchestrator：REST `/api/edge/*`（全局 token / 用户 token / agent 独立 token）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/edge/poll_for_task` | 心跳+领任务。body 分组见 [standards/edge-reporting.md §2](./standards/edge-reporting.md#2-心跳-body-字段分组规范)。返回 `{tasks: [...], task?, server_ts, max_concurrent, config_version, config, agent_token?, upgrade?}` |
| POST | `/api/edge/mark_started` | body：`task_id` → `{accepted}` |
| POST | `/api/edge/submit_result` | body：`task_id`, `status`, `mode?`, `exit_code?`, `stdout_tail?`, `stderr_tail?`, `artifacts[]?`, `duration_ms?`, `summary?`, `session_id?` → `{accepted}` |
| POST | `/api/edge/get_task_status` | body：`task_id` → `{found, task}` |
| POST | `/api/edge/task_log` | 实时输出上报（v1.4.0）。body：`task_id`, `entries: [{kind: text\|error\|complete\|raw, content}]` → `{accepted}` |

> 已移除的冗余接口：`/api/edge/dispatch_task`、`/api/edge/cancel_task`（控制面已有等价接口，探针从未使用）。

> **任务归属校验**：`submit_result` / `mark_started` / `get_task_status` / `task_log` 不只验 token——global token 全权；user token 需为任务所有者或同团队；agent 独立 token 仅能操作队列键等于自身 `device_id`/`agent_id` 的任务。越权写返回 403，越权读返回 `{"found": false}`（避免指令泄露）。

**认证（`require_any_token`，见 [auth-security.md §1](./auth-security.md)）接受三类凭据**，并返回调用者身份：
1. 全局 token（`AGENT_MESH_TOKEN`）→ `{auth: "global"}`；
2. 用户 token（API token 按 SHA-256 查表 / session token 按 HMAC 解析）→ `{auth: "user", ...}`；
3. **agent 独立 token**（`agents.token_hash` 查表，见 [auth-security.md §7](./auth-security.md#7-agent-独立-token-与设备-用户关联)）→ `{auth: "agent", agent_id, device_id}`，且**绑定 device_id**：用它以其他设备身份轮询会被拒（403）。

产物上传不在此前缀下：`POST /api/artifacts/{task_id}`（multipart `files`，任选 token）。上传/读取均校验调用方对该任务的访问权（同上一段的归属规则；任务不存在返回 404）。边沿把上传返回的 `ArtifactRef` 放进 `submit_result.artifacts`，orchestrator 同步落库（`INSERT OR IGNORE` 幂等）。

### 3.3 查询/管理接口 REST `/api/*`（用户 token，除非标注）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | **公开**。账号密码 → `{username, token, role}` |
| GET | `/api/auth/me` | 当前用户信息 |
| GET | `/api/healthz` | **公开**。健康检查 → `{status:"ok"}` |
| GET | `/api/tasks` | 任务列表（`agent_id?`, `status?`, `mode?`(`command`/`llm`), `search?`(指令/任务ID/节点模糊), `started_after?`/`started_before?`(ISO 时间, 开始时间区间), `limit` 1–1000, `offset` 分页；返回 `total` 总数供分页/跨页选择） |
| GET | `/api/tasks/{task_id}` | 任务详情 |
| GET | `/api/tasks/{task_id}/status` | 任务状态（`{found, task}`） |
| GET | `/api/tasks/{task_id}/logs` | 任务实时执行输出（`?after_id=&limit=`，增量；v1.4.0） |
| GET | `/api/tasks/{task_id}/events` | 任务审计事件时间线（`?limit=`；`dispatched`/`cancelled`/`permission_denied` 等） |
| POST | `/api/tasks/{task_id}/cancel` | 终止任务（`{accepted}`） |
| DELETE | `/api/tasks/{task_id}` | 删除单个任务（含 queue/results/artifacts 关联行） |
| POST | `/api/tasks/batch-delete` | 批量删除：`{task_ids: [...]}` 按 id，或 `{all_matching: true, status?, mode?, search?, started_after?, started_before?, agent_id?}` 删除匹配筛选的全部任务 |
| POST | `/api/tasks/dispatch` | 派发任务（REST 版） |
| GET | `/api/agents` | 所有节点 |
| GET | `/api/agents/{agent_id}` | 单个节点（数字 id / device_id / 显示名） |
| GET | `/api/agents/{agent_id}/detail` | 详情 + 最近 20 任务 |
| PATCH | `/api/agents/{agent_id}/alias` | 设置/清除别名 |
| PATCH | `/api/agents/{agent_id}/llm_config` | 设置节点级 LLM 配置（随心跳同步到该节点，优先级高于全局，见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)） |
| PATCH | `/api/agents/{agent_id}/access` | 设置节点访问控制 ACL（admin；`{teams?: [...], users?: [...]}`） |
| POST | `/api/agents/batch/template` | 批量绑定模板（Web UI，owner/admin；逐节点跳过无权/被锁定者，返回 `{applied, skipped}`） |
| POST | `/api/agents/batch/access` | 批量设置节点 ACL（admin；`{agent_ids, teams?, users?, mode: add\|remove\|set}` → `{updated}`） |
| POST | `/api/agents/{agent_id}/upgrade` | 请求升级节点（见 [auth-security.md §8](./auth-security.md#8-agent-自升级)；空闲时自动执行，失败自动回滚） |
| POST | `/api/agents/{agent_id}/token` | 轮换 agent 独立 token（返回一次，旧 token 立即失效，见 [auth-security.md §7](./auth-security.md#7-agent-独立-token-与设备-用户关联)） |
| DELETE | `/api/agents/{agent_id}` | 删除节点（下发自毁命令 + 立即删记录，见 [task-and-execution.md §4.2](./task-and-execution.md#42-apitaskspy--apiagentspy)） |
| POST | `/api/artifacts/{task_id}` | 产物上传（**任意 token**） |
| GET | `/api/artifacts/{task_id}` | 列出产物 |
| GET | `/api/artifacts/{task_id}/{artifact_id}` | 下载产物 |
| GET | `/api/settings` | 全局配置（`public_url` / `llm_api_key` / `llm_base_url` / `llm_model`） |
| PATCH | `/api/settings` | 更新全局配置（admin） |
| GET/POST/PATCH/DELETE | `/api/teams*` | 团队/组织 CRUD 与成员（admin；用户归属单一团队）。含 `POST /api/teams/{team_id}/members`、`DELETE .../members/{user_id}`、`PUT /api/teams/{team_id}/nodes`（全量替换团队可见节点） |
| GET/POST/PATCH/DELETE | `/api/templates*` | 模板 CRUD（`require_ui_user`：**必须携带 `X-Agent-Mesh-UI: 1`**，否则一律 404；Web UI 专用，不对外开放） |
| GET | `/api/bootstrap/install.sh` | 生成新节点安装命令脚本（内嵌 orchestrator `base_url`；**不再内嵌 LLM 配置**，新节点注册后由心跳 config-sync 下发） |
| GET | `/api/bootstrap/{filename}` | 下载安装包（`<db_path>/../bootstrap/` 下的文件） |
| GET | `/api/skills` | 技能摘要列表（`require_any_token`，仅 name/description/version/enabled） |
| GET | `/api/skills/{name}` | 单个技能摘要 |
| POST | `/api/skills` | 上传技能 zip（用户 token，提取并校验 SKILL.md frontmatter，重复上传 version+1） |
| PATCH | `/api/skills/{name}` | 启停用技能 |
| DELETE | `/api/skills/{name}` | 删除技能 |
| GET | `/api/skills/{name}/download` | 下载技能 zip（`require_any_token`，边沿可用自己的独立 token 拉取） |
| POST | `/api/files` | 上传文件到文件库（multipart `files`，用户 token，同 md5+文件名 去重返回原 file_id） |
| GET | `/api/files` | 文件库列表（用户 token，`search?` 按文件名/文件ID模糊） |
| GET | `/api/files/{file_id}` | 下载文件库文件（`require_any_token`，带 `X-File-Md5` 头） |
| POST | `/api/files/batch-delete` | 批量删除文件库文件（`{file_ids: [...]}`，用户 token，DB+磁盘） |
| POST | `/api/files/batch-download` | 批量下载为 ZIP（`{file_ids: [...]}`，用户 token；重名自动加 file_id 前缀） |
| DELETE | `/api/files/{file_id}` | 删除文件库文件（用户 token，不校验引用） |
| GET | `/api/skill-doc/agent-mesh` | 下载**个性化** agent-mesh SKILL.md（`require_user_token`）：自动填充配置的公开地址 + 当前用户 token，未配置地址时给出索取提示） |
| GET | `/` | Web 看板（index.html） |
| GET | `/static/*` | 看板静态资源 |

### 3.4 文件库与任务附件

文件库独立于任务存储（镜像技能库的「上传-引用-按需下载」模式）：

1. **上传**：`POST /api/files`（multipart `files`，可多文件，用户 token）。服务端计算 md5、落盘到 `<db_path 父目录>/files/<file_id>_<文件名>`（`file_id = f-<uuid8>`）并入库；**同 md5+文件名 自动去重**返回已有 `file_id`。响应为权威 `FileRef`：`{file_id, filename, size, content_type, md5, download_url}`。
2. **引用**：REST `POST /api/tasks/dispatch` 的 `attachments: [file_id]` 引用文件库文件；服务端按 id 解析成 `FileRef` 快照写入 `tasks.attachments`（JSON 列），id 无效报错。非 admin 只能引用自己创建的文件。
3. **下载**：探针领到任务后、执行前，按 `download_url` 把每个附件下载到工作目录并以原文件名落盘，流式计算 md5 与快照比对；**下载失败或 md5 不符 → 任务标记失败**（`exit_code=-3`、`stderr_tail="attachment download failed: <文件名>: <原因>"`）。下载发生在执行前快照之前，附件不会被收集为产物。
4. **管理**：`GET /api/files` 列表（`search` 模糊）、`GET /api/files/{file_id}` 下载、`DELETE /api/files/{file_id}` 删除（不校验引用；被删文件的任务执行时会下载失败）、`POST /api/files/batch-delete` 批量删除、`POST /api/files/batch-download` 批量打包下载。Web 看板「文件」页提供上传/搜索/列表/下载/批量删除/批量下载。
5. **删除文件 → 引用它的任务**：执行时下载 404 → 按第 3 条标记失败。
