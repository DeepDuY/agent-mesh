# LLM 会话复用、技能库与资源指标

> 来源：原 DESIGN.md §15。

## 1. LLM 会话复用（选填）

- **回传**：`opencode run --format json` 的每行事件带顶层 `sessionID`；边沿 `_extract_session_id()` 从输出中取最后一个值，写入 `ExecutionOutcome.session_id` → `TaskResult.session_id` → `task_results.session_id`（迁移 009）。
- **续用**：派发任务时 `Constraints.session_id`（选填，存 `tasks.session_id`）命中后，边沿执行 `opencode run --command - --session <id> ...` 续用上次会话；不填则新开会话。主 Agent 流程：读上次任务 `result.session_id` → 新任务带上 `session_id`。

## 2. 技能库（zip 上传 + 摘要 + 按需下载）

- **存储**：`skills` 表（name 主键、description、version、enabled、filename）；zip 落盘 `<db_path>/../skills/<name>/<name>.zip`。
- **上传**：`POST /api/skills`（multipart zip，用户 token）→ 内存解析 zip 提取 `SKILL.md` → 校验 frontmatter（`name` 匹配 `^[a-z0-9]+(-[a-z0-9]+)*$`、`description` 非空）→ 落盘 + upsert 元数据（重复上传 `version+1`，边沿可感知变更）。
- **摘要接口只暴露元数据**：`GET /api/skills` / `/api/skills/{name}`（`require_any_token`）仅返回 name/description/version/enabled，**永不包含 SKILL.md 正文**。
- **按需下载**：`GET /api/skills/{name}/download`（`require_any_token`，边沿用自己的独立 token 拉取完整 zip）。
- **提示词注入**：`_wrap_llm_instruction(instruction, system_prompt="")` 在提示词顶部注入「## 角色与上下文」块，内容为**节点 `system_prompt` + 绑定模板 `system_prompt` 的拼接**（节点在前；无全局默认）；随后追加技能库使用指引，引用 `$EDGE_TOKEN` / `$ORCHESTRATOR_URL`（边沿在 opencode 子进程 env 注入，token 不明文进提示词）；由 agent 自主决定浏览摘要 → 下载 zip → `unzip -d .opencode/skills/<name>/` → `cat SKILL.md` 按说明执行。`build_opencode_config` permission 增加 `"skill": "allow"`。
- **管理端**：Web 看板「技能」页（上传/启停/下载/删除）；MCP 工具 `list_skills` 供主 Agent 浏览后决定是否在指令中提示使用。

## 3. 资源指标心跳上报

- 边沿 `_collect_metrics()` 用 `psutil`（依赖，PyInstaller 自动打包）采集：`cpu_percent`（`cpu_percent(interval=None)` 差值）、`mem_percent` / `mem_used_mb` / `mem_total_mb`；采集失败回退 `None`。
- 随 `poll_for_task` 上报，`heartbeat()` 以注册表 `METRIC_FIELDS` 持久化到 `agents` 表（每次心跳覆盖，见 [standards/edge-reporting.md](./standards/edge-reporting.md)）；`AgentStatus` 暴露并在 Web 节点详情「系统资源」区展示（探针版本即 `version` 字段）。

## 4. 发行版上报（distro）

- 边沿 `get_distro()` 读 `/etc/os-release` → `ID VERSION_ID`（如 `ubuntu 22.04`），非 Linux 返回 `None`。
- 随 `poll_for_task` 以注册表 `SYSTEM_FIELDS` 持久化到 `agents.distro`（缺失保留旧值）；Web 看板「系统」列显示 `distro || os`（老节点兜底 `linux`）。
- 完整规范见 [standards/edge-reporting.md](./standards/edge-reporting.md)。

## 5. 文件库（任务附件）

文件库独立于任务（镜像技能库的「上传-引用-按需下载」模式），让主 Agent 给任务附带文件：

- **存储**：`files` 表（file_id 主键、filename、size、content_type、md5、created_by）；文件落盘 `<db_path 父目录>/files/<file_id>_<文件名>`（`file_id = f-<uuid8>`）。
- **上传**：`POST /api/files`（multipart `files`，可多文件，用户 token）→ 计算 md5 入库；**同 md5+文件名 去重**返回已有 `file_id`；响应含 `{file_id, filename, size, content_type, md5, download_url}`。
- **引用**：REST `POST /api/tasks/dispatch` 与 MCP `dispatch_task` 的 `attachments: [file_id]` → 服务端解析成 `FileRef` 快照存 `tasks.attachments`（JSON 列），id 无效报错。**MCP 无上传工具**（二进制只能走 REST）。
- **下载与校验**：探针执行前把附件下载到工作目录（原文件名），流式 md5 与快照比对；**失败/不符 → 任务标记失败**（`exit_code=-3`、`summary="attachment download failed"`）。下载在 `_snapshot_files` 之前，附件不会误收成产物；llm 提示词追加"工作目录可能已放入附件"提示。
- **管理**：Web 看板「文件」页（搜索/上传/列表/下载/删除/批量删除/批量下载为 ZIP）；`DELETE /api/files/{file_id}` 不校验引用（被删文件的任务执行时下载失败而标记失败）。配置页可下载**个性化** agent-mesh SKILL.md（`GET /api/skill-doc/agent-mesh`，自动填充公开地址 + 当前用户 token，未配置地址时给出索取提示）。

## 6. 多任务并发执行（v1.4.0）

- **并发上限**：全局设置 `max_concurrent`（默认 `2`，配置页或 `PATCH /api/settings` 调整），随心跳 `poll_for_task` 响应顶层 `max_concurrent` 下发到边沿。
- **协议**：边沿心跳请求带 `running_tasks: [task_id...]`（当前正在执行的任务）；响应从单 `task` 扩展为 `tasks: [...]` 数组（`task` 字段保留 = `tasks[0]`，兼容旧版探针）。
- **orchestrator 分配**（`task_store.heartbeat`）：以 `tasks` 表 assigned/working 状态统计活跃数；`resume = 活跃但不在 running_tasks 的`（边沿重启恢复）；`capacity = max_concurrent - 活跃数`，循环 dequeue+assign 直到满或队列空，返回 `resume + new`。
- **边沿异步执行**（`edge/agent.py`）：`_loop` 对每个新任务 `asyncio.create_task` 并发执行（`self._running` 去重 + 计数），不再串行 `await`；升级仅在 `_running` 为空（完全空闲）时执行；取消监控 `_monitor_cancel` 为 per-task，天然适配并发。
- **workdir 隔离**：任务未显式指定 `workdir` 时落到 `<EDGE_WORKDIR>/tasks/<task_id>/` 独立子目录（opencode.json、附件、产物互不干扰）；显式 workdir 由调用方自行隔离。
- **离线清扫**：`_sweep_offline` 对 agent 所有 assigned 任务回队列（不再只处理单 `current_task_id`）；有任意 working 任务则保持在线。
- **队列原子化**：SQLite `dequeue` 改为 `DELETE ... RETURNING` 单语句，跨 worker 不再竞态（PG 本就是 `FOR UPDATE SKIP LOCKED`）。

## 7. LLM 实时执行输出（v1.4.0）

- **上报**：边沿解析 opencode `--format json` 的 JSONL 事件流（`type=text`→`text`、`type=error`→`error`、`type=complete`→`complete`、其余→`raw`），经 `asyncio.Queue` 批量（~0.5s）POST `POST /api/edge/task_log`（`require_any_token`）；command 模式将 stdout/stderr 行按 `raw` 上报。上报失败仅 warning，不影响最终结果。
- **存储**：`task_logs` 表（id 自增/task_id/kind/content/created_at，迁移 012）；删除任务级联清日志。
- **查询**：`GET /api/tasks/{task_id}/logs?after_id=&limit=`（用户 token）增量拉取；MCP 工具 `get_task_logs(task_id, after_id)`。
- **看板**：任务详情弹窗新增「实时输出」区，执行中每 2s 轮询增量渲染（text 普通/error 红/complete 绿），关闭弹窗自动停止轮询。
