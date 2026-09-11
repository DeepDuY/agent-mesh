# 任务模型、状态机与边沿 Agent 执行

> 来源：原 DESIGN.md §5 / §6 / §9。

## 1. 任务模型与双执行模式

任务 `Task.mode` **必填**，二选一：

### 1.1 command 模式（确定性执行）

- `instruction` 作为 shell 命令**原样**执行：`bash -c <instruction>`，**不经过 LLM**。
- 适用：跑命令、脚本、测试、安装等确定性任务（含删除节点的自毁命令）。
- 产物：只收集任务期间**新创建**的文件（`_collect_artifacts` 对执行前快照做差集，跳过以 `.` 开头的隐藏路径）。

### 1.2 llm 模式（自然语言任务）

- `instruction` 作为自然语言任务，交给边沿节点的 opencode（或 claude）运行时执行。
- opencode 以结构化 JSON 输出，固定字段：`summary`（摘要）、`answer`（回答）、`artifacts`（声明的产物列表）。
- **产物收集策略（含回退）**：
  - 若成功解析出结构化输出 → 只上传 JSON 中 `artifacts` 声明的文件。
  - 若解析失败（非 JSON 输出）→ **回退为收集工作目录中所有新创建的文件**。
- LLM 配置注入方式：executor 把 `build_opencode_config()` 生成的配置写入 `workdir/opencode.json`，并通过环境变量 `OPENCODE_CONFIG=<该文件路径>` 传给 opencode 子进程；执行结束 `finally` 删除该临时配置。任务级隔离，不写全局配置文件。

### 1.3 产物策略对比

| 模式 | 产物收集 |
|------|----------|
| command | 工作目录中任务期间**新创建**的文件 |
| llm | 结构化输出中 `artifacts` 声明文件；解析失败时回退收集全部新文件 |

### 1.4 产物存储（orchestrator 侧）

- 磁盘：`<artifact_dir>/<task_id>/<artifact_id>_<原始文件名>`，`artifact_id = a-<uuid8>`。
- 元数据：`artifacts` 表（`INSERT OR IGNORE` 幂等）。
- 下载：`GET /api/artifacts/{task_id}/{artifact_id}` 用 `FileResponse` 返回，`content_type` 由 `mimetypes` 猜测。
- 大小限制：单文件 `artifact_max_size_mb`（默认 50MB），总量 `artifact_max_total_mb`（默认 200MB）。

## 2. 任务状态机

```
                   ┌────────────────────────────────────────┐
                   │            (后台清扫协程, 每5s)         │
queued ──心跳领取──▶ assigned ──mark_started──▶ working     │
   ▲                  │  ▲                     │            │
   │                  │  │                     ├─超时───────▶│ timed_out
   │                  │  │                     │  超过       │    │
   │ 离线回队          │  └─断线重连：直接返回   │  timeout_s  │   └─ retry_count<max_retries
   │ (assigned且心跳   │   当前 assigned/working│            │        │ 回 queued(重试+1)
   │  超时)            │                        │            │        ▼（否则终态）
   │                  ▼                        ▼            │
   └──────────────── completed / failed  ◀─────┘            └── timed_out
                        │  ▲
                        │  │
   queued/assigned/working ──cancel_task──▶ cancelled (终态)
```

- 领取：`queued → assigned`（心跳 `dequeue` 命中且任务仍为 queued）。
- 开始：`mark_started()`：`assigned → working`，写 `started_at`。
- 结束：`submit_result()`：**`assigned` 或 `working`** → `completed / failed`（写 `task_results` 行、状态、`finished_at`，清 `current_task`）。
- 超时扫描：`working` 且 `started_at + timeout_s <= now` → `timed_out`；**以原子条件更新（`update_task_status(..., expected_status='working')`）标记超时**，边沿并发提交的真实结果（已完成/失败/取消）不会被清扫器覆盖；若 `retry_count < max_retries` 则回 `queued` 并 `retry_count+1`、重置 assigned/started/finished、重新入队（**仅超时触发重试，失败不重试**）。
- 离线扫描：`assigned` 但心跳超时 → 回 `queued` 并重新入队；`working` 的任务不会被误判离线（节点仍在执行，保持在线）。
- **终止**：`cancel_task()` 只对非终态任务生效——`queued` 直接从队列移除；`assigned/working` 置为 `cancelled`（终态），边沿 agent 通过执行期间的 cancel 监视协程（`agent.py:_monitor_cancel`）轮询到 `cancelled` 后终止正在运行的子进程，且不再提交结果。
- 队列与 `agents.current_task_id` 均以节点 **device_id（稳定键）** 匹配。

## 3. 边沿 Agent 内部逻辑

### 3.1 agent.py：主循环

- `EdgeAgent(agent_id, orchestrator_url, token, runtime, workdir, llm_api_key, llm_base_url, llm_model, heartbeat_s=3, install_dir)`。
- `orchestrator_url.replace("/mcp", "")` 得到 REST base URL（兼容 `EdgeConfig` 默认值带 `/mcp` 后缀）。
- 启动时 `read_agent_version(install_dir)` 读取已装版本（缺省回退内置 `VERSION`）；若 `token` 为空/占位且 `edge.env` 已持久化独立 token 则回退读取（见 [auth-security.md §7](./auth-security.md#7-agent-独立-token-与设备-用户关联)）。
- `_loop()`：
  1. `device_id = get_device_id(install_dir)`（首次运行持久化）。
  2. 每 `heartbeat_s`（默认 3s）调 `poll_for_task`，上报 `agent_id/device_id/runtime/hostname/os/distro/arch/version`、资源指标及 `running_tasks`（当前正在执行的任务 id，用于并发重连恢复；上报字段规范见 [standards/edge-reporting.md](./standards/edge-reporting.md)）。
  3. **收到 `agent_token`（仅首次注册返回一次）→ `persist_edge_token()` 写入 `edge.env` 并 `client.set_token()` 切换为独立 token 认证（见 [auth-security.md §7](./auth-security.md#7-agent-独立-token-与设备-用户关联)）**。
  4. 读响应 `max_concurrent`（服务端并发上限，默认 2）更新本地并发数。
  5. 对 `tasks` 数组里每个新任务 `asyncio.create_task(_execute(task))` 并发执行（`self._running` 去重 + 计数，达到上限则留给下轮心跳）；旧版 orchestrator 单任务响应（`task` 字段）同样兼容。
  6. `_confirm_upgrade_healthy()`：首次成功心跳清除 `etc/upgrading`、`etc/upgrade-started`、`.bin.old`、`agent_version.bak`（见 [auth-security.md §8](./auth-security.md#8-agent-自升级)）。
  7. **收到 `upgrade` 指令且完全空闲（`_running` 为空、本轮无新任务）→ `_perform_upgrade()`（见 [auth-security.md §8](./auth-security.md#8-agent-自升级)）**。
  8. SIGTERM/SIGINT 置 `_stop_event` 退出。
- `_execute(task)`：
  1. 创建 `cancel_event` 并启动 `_monitor_cancel(task_id, cancel_event)` 后台协程，每 `heartbeat_s` 轮询任务状态，检测到 `cancelled` 则置事件（per-task，天然适配并发）。
  2. `mark_started(task_id)`（失败仅告警，不中断）。
  3. **下载任务附件**：建 workdir → 按 `task.attachments` 的 `download_url` 逐个下载到 `workdir/<文件名>`，流式 md5 与快照比对（`rest_client.download_file`）。**失败或 md5 不符 → 提交 `failed`（`exit_code=-3`、`stderr_tail="attachment download failed: <文件名>: <原因>"`）并返回，不执行任务**（下载在 `_snapshot_files` 之前，附件不会误收为产物）。
  4. `executor.run_task(task, cancel_event, log_callback=...)`：执行期间把实时输出经 `log_callback` 批量上报（LLM 事件分类 text/error/complete/raw，command 输出按 raw；见 [features.md §7](./features.md#7-llm-实时执行输出v140)）。
  5. 若 `cancel_event.is_set()`：任务已被取消，**不提交结果**（任务已是终态），直接返回。
  6. 否则按 `outcome.artifacts_paths` 从 `workdir` 读取产物字节 → `upload_artifacts(task_id, files)` → 把返回的 `ArtifactRef` 写回 `outcome.artifacts`。
  7. `executor.build_task_result(outcome)` → `submit_result(...)`。
  8. 任何异常：补报 `failed` 结果（`exit_code=-2`, `summary="edge execution exception"`）。

### 3.2 rest_client.py

- `httpx.AsyncClient`，统一 `Authorization: Bearer <token>`，超时 30s。
- `_post(base/api/edge/*, json)` 与 multipart 上传 `base/api/artifacts/{task_id}`。
- 额外提供 `get_task_status()`（cancel 监视轮询用）。
- `set_token(token)`：切换全局请求的 Bearer token（收到 agent 独立 token 后使用）。
- `download_to(url, dest)`：流式下载升级包（长超时 600s，见 [auth-security.md §8](./auth-security.md#8-agent-自升级)）。

### 3.3 execution/ 包：任务执行

按职责拆分为 `edge/execution/` 包：

- `common.py`：`ExecutionOutcome` 数据结构 + 共享工具。含 `_wait_proc()`（并发等待子进程完成/超时/取消：收到取消信号先 `kill` 再 `SIGKILL`，返回 `(-2, True)`）。
- `command.py`：`run_command()`。`bash -c <instruction>`，执行前 `_snapshot_files(workdir)`；滚动截断 stdout/stderr 到 `output_limit`（保留尾部）；超时 `timeout_s` 强杀，`exit_code=-1`；`_collect_artifacts` 收集新文件；summary 取输出首行。
- `llm.py`：`run_llm()`。
  - `runtime=opencode`：`opencode run --command - --format json --auto --dir <workdir>`，stdin 传 `_wrap_llm_instruction()`（要求输出固定 JSON：`summary`/`answer`/`artifacts`）。
  - `runtime=claude`：`claude -p <instruction>`。
  - 写 `workdir/opencode.json`（`build_opencode_config()` 输出，含 provider/permission），设 `env["OPENCODE_CONFIG"]`，结束删除。
  - 输出解析：优先解析 opencode JSONL 事件流里的 `text` part（含 ```json 围栏）→ `_extract_structured_output()`；失败则回退收集全部新文件。
  - 超时/取消处理同 command。
- `__init__.py`：`Executor` 门面类 + `resolve_workdir()`。**workdir 解析**：优先用任务 `Constraints.workdir`；未指定（空或默认 `.`）时落到 `<EDGE_WORKDIR>/tasks/<task_id>/` 独立子目录（v1.4.0 并发隔离，opencode.json/附件/产物互不干扰；显式 workdir 由调用方自行隔离），再 `.expanduser().resolve()` 成绝对路径并 `mkdir`。`_execute` 的附件下载与产物收集也用同一函数，保证三处目录一致。`run_task(task, cancel_event, log_callback=None)` 按 mode 分发到 command/llm（`log_callback` 用于实时输出上报）。
- `_classify_llm_error()`：把常见上游错误（无法连接/认证失败/超时）映射为中文提示写入 summary。

### 3.4 config_writer.py：设备标识、系统信息、LLM 配置、独立 token、wrapper

- `get_device_id()`：① `/etc/machine-id`；② `<install_dir>/machine-id`（首次生成 `m<毫秒时间戳>-<pid>` 并持久化）；③ 回退 `sha256(hostname)`。
- `get_arch()` / `get_os()` / `get_distro()`：上报节点架构、系统（`linux`/`darwin`/`win32`）与发行版（如 `ubuntu 22.04`）。采集规范见 [standards/edge-reporting.md](./standards/edge-reporting.md)。
- `read_agent_version(install_dir)`：读 `etc/agent_version`（安装/升级时写入，用于版本上报与升级判定）。
- `apply_llm_config(install_dir, config, version)`：LLM 配置同步持久化——更新 `etc/edge.env` 的 `EDGE_LLM_*`（缺失追加）并写 `etc/config_version`（见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)）。
- `persist_edge_token(install_dir, token)` / `read_edge_token()`：把 agent 独立 token 写/读 `edge.env` 的 `EDGE_TOKEN`（见 [auth-security.md §7](./auth-security.md#7-agent-独立-token-与设备-用户关联)）。
- `WRAPPER_SCRIPT`：回滚感知的启动 wrapper（`bin/agent-mesh-edge` → `exec bin/agent-mesh-edge.bin`），含两阶段升级回滚逻辑（见 [auth-security.md §8](./auth-security.md#8-agent-自升级)）。
- `build_opencode_config(api_key, base_url, model, allowed_tools, models)`：`provider.anthropic` + `models`（**无硬编码**，完全由 orchestrator 配置的 `llm_models` 生成：map 键 = 去 `anthropic/` 前缀的完整 id、`name` = `_canonical_model_id()` 末段；请求的 `model` 也会 `setdefault` 补入）；模型为空时 `run_llm` 直接返回 `no LLM model configured`，不启动运行时；`permission` 默认 `edit/bash: allow`、`webfetch: ask`、`mcp__*: deny`，`allowed_tools` 追加 `allow`。
- `find_runtime()`：`shutil.which` → `~/.opencode/bin/{runtime}` 回退。

## 4. orchestrator 侧任务处理（api/edge.py 之外）

### 4.1 api/edge.py

REST 边沿协议（`poll_for_task`/`submit_result`/`mark_started`/`get_task_status`），详情见 [protocol.md §3.2](./protocol.md#32-边沿-agent--orchestratorrest-apiedge全局-token--用户-token--agent-独立-token)。`poll_for_task` 上报字段经 `extract_telemetry()` 白名单处理。

### 4.2 api/tasks.py / api/agents.py

- `POST /api/tasks/dispatch`：REST 版派发（控制面，用户 token）。
- `DELETE /api/agents/{id}`：先 `store.dispatch()` 下发 **command 模式**自毁命令（停/禁 systemd、删 service 文件、删 `${EDGE_INSTALL_DIR:-/opt/agent-mesh-agent}`（边沿进程继承 `EDGE_INSTALL_DIR` 环境变量，兼容非默认安装目录），最后 `systemctl stop`，`timeout_s=30`，`workdir=/tmp`）；随后**立即** `delete_agent(id)` 删 DB 记录。不等待任务结果（命令会停掉 agent 自身）。
