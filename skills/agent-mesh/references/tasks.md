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
| 一次发给多台机器 | 「给这几台都装一下」「批量巡检后汇总」 | `POST /tasks/dispatch-batch` 一次派发成一批（run），再 `GET /runs/{run_id}` 看整批（见下） |

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
| `depends_on_run` | 一个批次 run_id；等价于把该批次**所有任务 id** 放进 `depends_on`。做「等这一批全跑完再汇总」最方便 |
| `attachments_from` | 上游任务 id 列表；把这些任务的**产物**自动转成附件下发，省去「下载再上传」 |
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

## 批量派发（批次 / Run）

一次请求把**同一条任务**发给多个节点，它们共享一个 `run_id`（批次）。

```bash
curl -s -X POST "$AGENT_MESH_BASE_URL/tasks/dispatch-batch" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -H "Idempotency-Key: deploy-2026-09-24" \
  -d '{
        "targets":[3,7,12],
        "mode":"command",
        "instruction":"systemctl restart myapp && systemctl is-active myapp",
        "timeout_s":120,
        "max_retries":1
      }'
# => {"run_id":"r-9f3a","queued":3,"task_ids":["t-...","t-...","t-..."],"denied":[]}
```

- `targets`：节点引用数组（数字 id / device_id / 显示名都可），会**去重**。整批有扇出上限（默认 200），单节点也有每批上限（默认 50）——超限的目标进 `denied`。
- 每个目标**独立校验**（节点权限、模型、技能、命令策略）：不合格的进 `denied`（含 `reason`；命令被拦截时还带 `task_id`），**不影响**其余目标，返回仍是 `200`。所以要看 `queued` 与 `denied` 两个字段。
- 可选参数与单任务一致（`workdir`/`timeout_s`/`model`/`skills`/`attachments`/`max_retries`/`depends_on`），另有 `run_id`（自定义批次号）、`depends_on_run`、`attachments_from`。
- **幂等**：带 `Idempotency-Key` 头（或 body 的 `idempotency_key`）重复提交，只会返回同一个 `run_id`（`duplicate: true`），不会重复派发。网络抖动重试时务必带上。

### 看整批（Run 聚合）

```bash
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/runs/r-9f3a"
# => {"run_id":"r-9f3a","total":3,"counts":{"completed":2,"failed":1},"active":false,
#     "tasks":[{"task_id":"t-..","agent_id":"..","status":"failed","exit_code":1,"summary":".."}, ...]}
```

- **一次调用拿到整批进度**：`counts` 是各状态数量，`tasks` 是每任务**一行紧凑摘要**（不含完整日志）。要看某台完整日志再单独 `GET /tasks/{id}/logs`。
- `?status=failed` 只看失败项；`?include_tasks=false` 只要计数。
- **长轮询**：`?wait=30` 阻塞最多 30 秒，直到**任一任务状态变化**或整批结束才返回——比「每 2 秒轮询」省调用，适合主 Agent 等一批任务。
- `POST /runs/{run_id}/cancel` 终止整批；`POST /runs/{run_id}/retry` 重跑批内失败/超时的任务。

### 典型模式：先扇出、再汇总（fan-out / fan-in）

1. `POST /tasks/dispatch-batch` 扇出一批 map 任务，拿到 `run_id`。
2. 建一个汇总任务，`depends_on_run` = 这个 run_id（或直接 `depends_on` 那批 id）。编排器会在**全部完成**后自动派发它；任一失败则级联取消汇总任务。
3. 需要把某台的产物喂给汇总任务时，用 `attachments_from` 引用上游任务 id。

列表也可按批次筛：`GET /tasks?run_id=r-9f3a`。

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
# 列表（分页+筛选）：status / mode / run_id / search / started_after / started_before / agent_id / limit / offset
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
- 任务对象含 `dispatched_by`（派发者用户名）与 `user_id`/`team_ids`（并保留 `team_id`=第一个团队），可用于区分多用户来源。
- 只能看到/操作自己（或所属团队）的任务；无权的任务返回 404。

## 完整流程（推荐节奏）

**单台任务：**

1. `GET /agents` → 选 `online=true` 且 `effective_description` 匹配的节点，记下数字 `id`（[nodes.md](nodes.md)）。
2. 若要用本地文件：`POST /files` 上传拿 `file_id`（[files.md](files.md)）。
3. `POST /tasks/dispatch` → 拿 `task_id`。
4. 需要进度：`GET /tasks/{task_id}/logs?after_id=<next_id>` 增量看输出。
5. 每 2–3 秒 `GET /tasks/{task_id}/status` 直到终态；用户要停就 `POST /tasks/{task_id}/cancel`。
6. 读 `task.result`；有 `artifacts` 就下载给用户（[files.md](files.md)）。

**多台 / 批量：** 用 `POST /tasks/dispatch-batch` 拿 `run_id`，用 `GET /runs/{run_id}`（可 `?wait=30`）一次看整批；需汇总时再建一个 `depends_on_run` 的汇总任务（见上「批量派发」）。
