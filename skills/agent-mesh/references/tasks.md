# 任务：派发、监控、取消、审计

任务是最核心的能力：**在远程节点上执行东西**。分两种模式：

- **`command`**：`instruction` 是一条 shell 命令（`bash -c` 原样执行），**不经过 LLM**。适合确定性的操作：部署、跑测试、看系统信息、文件处理。工作目录下**新增的文件会自动作为产物**收集。
- **`llm`**：`instruction` 是**自然语言任务**，交给节点本机的 opencode 执行。适合需要推理/多步/写代码的模糊任务。需要节点已配置可用模型。

> 能用明确命令做的事，优先 `command`（更快、更可控、更省 token）；需要「动脑」才用 `llm`。

## 何时用

| 用户诉求 | 用户可能的说法 | 用这个 |
|---|---|---|
| 在远程机器执行命令 | 「跑一下 `df -h`」「执行这个脚本」「部署到那台机器」「在那台机器上装个依赖」 | `POST /tasks/dispatch`，`mode=command` |
| 让远程的 AI 干活 | 「让远程那台帮我改代码/排查日志/分析数据/写个脚本」 | `POST /tasks/dispatch`，`mode=llm` |
| 带文件的远程任务 | 「用这个 csv 生成报表」「把这份配置传过去改一下」 | 先 `POST /files` 上传，再带 `attachments` 派发（见 [files.md](files.md)） |

## 派发

```bash
set -a; . ./.env; set +a
curl -s -X POST "$AGENT_MESH_BASE_URL/tasks/dispatch" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"command","instruction":"echo hello && uname -a","timeout_s":120}'
# => {"task_id":"t-xxxxx","status":"queued"}
```

- `agent_id` 用 [nodes.md](nodes.md) 里查到的数字 `id`（也接受 device_id / 显示名）。
- 派发前服务端会校验：节点存在、你有权访问、模型命中可用列表、command 权限策略。被拒时返回 `403`（detail 说明原因）；该次尝试仍会以 `denied`（**已拦截**）状态出现在任务列表里，摘要即拒绝原因，便于追溯。

### 常用可选参数

| 参数 | 说明 |
|------|------|
| `workdir` | 工作目录。**不填**则自动落在独立子目录 `<EDGE_WORKDIR>/tasks/<task_id>/`（推荐，任务间互不干扰） |
| `timeout_s` | 超时秒数，默认 300；超时任务置 `timed_out` |
| `model` | 仅 llm：指定模型，须命中可用模型列表；不填用节点/模板/全局默认 |
| `max_retries` | 超时后自动重试次数 |
| `depends_on` | 依赖的任务 id 列表。依赖全部 `completed` 后才派发；任一依赖失败/终止会**级联取消**本任务（摘要与事件记录原因）；派发时校验依赖存在与环 |
| `session_id` | 仅 llm：续用上一个 llm 任务的会话 |
| `attachments` | 文件库 file_id 列表，见 [files.md](files.md) |
| `skills` | 技能名列表；服务端校验存在且启用，边沿拉取并注入其内容（见 [skills.md](skills.md)） |
| `output_limit` | 输出截断字节数，默认 200000 |

**续用 LLM 会话**：先读上一任务 `result.session_id`，派发时带上 `session_id`；本次返回的 `result.session_id` 为实际会话 ID。

## 监控

状态机：`queued → assigned → working → completed / failed / timed_out`，另有 `cancelled`（人为终止）与 `denied`（派发被权限策略拦截，从未执行）。

```bash
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/tasks/t-xxxxx/status"
```

终态后读 `task.result`（`summary` / `exit_code` / `stdout_tail` / `stderr_tail` / `artifacts` / `session_id`）。轮询间隔建议 2–3 秒。

### 实时输出

```bash
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks/t-xxxxx/logs?after_id=0"
# => {"task_id":"...","logs":[{"id":1,"kind":"text","content":"..."}],"next_id":2}
# 下次用 after_id=next_id 增量拉取
```

`kind`：`text`（LLM 文本）/ `error` / `complete` / `raw`（command 原始行）。

## 取消

```bash
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks/t-xxxxx/cancel"
```

- `queued`：移出队列，不执行。
- `assigned` / `working`：edge 在下一个取消监视轮询（约一个心跳周期，默认 3s）发现后杀掉整个进程组（SIGTERM→SIGKILL），不提交结果。
- 已终态：返回 `{"accepted": false}`（幂等）。

## 查找 / 清理 / 审计

```bash
# 列表（分页+筛选）：status / mode / search / started_after / started_before / agent_id / limit / offset
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks?status=working&mode=llm&search=hello&limit=50"
# 单任务详情
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/tasks/t-xxxxx"
# 审计事件（dispatched / cancelled / permission_denied）
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/tasks/t-xxxxx/events"
# 批量删除（按 id 或按条件）
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"all_matching":true,"status":"cancelled"}' "$AGENT_MESH_BASE_URL/tasks/batch-delete"
```

- `search` 匹配指令/任务 id/节点/派发者用户名；`started_after` / `started_before` 为 ISO 时间。
- 任务对象含 `dispatched_by`（派发者用户名）与 `user_id`/`team_id`，可用于区分多用户来源。
- 只能看到/操作自己（或本团队）的任务；无权的任务返回 404。

## 完整流程（推荐节奏）

1. `GET /agents` → 选 `online=true` 且 `effective_description` 匹配的节点，记下数字 `id`（[nodes.md](nodes.md)）。
2. 若要用本地文件：`POST /files` 上传拿 `file_id`（[files.md](files.md)）。
3. `POST /tasks/dispatch` → 拿 `task_id`。
4. 需要进度：`GET /tasks/{task_id}/logs?after_id=<next_id>` 增量看输出。
5. 每 2–3 秒 `GET /tasks/{task_id}/status` 直到终态；用户要停就 `POST /tasks/{task_id}/cancel`。
6. 读 `task.result`；有 `artifacts` 就下载给用户（[files.md](files.md)）。
