# 单节点任务

`command` 模式的 `instruction` 为 shell 命令；`llm` 模式为交给远程 opencode 的自然语言任务，要求已配置模型。可明确指定命令时优先用 `command`；批量派发见 [批次](runs.md)。

## 派发

```bash
set -a; . ./.env; set +a
curl -sS -X POST "$AGENT_MESH_BASE_URL/tasks" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"command","instruction":"uname -a","timeout_s":120}'
# => {"task_id":"t-...","status":"queued"}
```

`agent_id` 优先使用 [节点列表](nodes.md)中的数字 `id`，也可使用 `device_id` 或节点注册时的 `agent_id`。常用可选字段：

| 字段 | 语义 |
|---|---|
| `workdir` | 工作目录；省略时探针使用该任务的隔离目录。 |
| `timeout_s`、`max_retries` | 超时秒数（默认 300）、超时后的最大自动重试次数。 |
| `model`、`session_id` | LLM 模型与已有会话 ID；可从上次 `task.result.session_id` 取得。 |
| `attachments` | 文件库 `file_id` 列表；上传方法见 [文件](files.md)。 |
| `skills` | 已启用的技能名列表；仅 LLM 任务使用，见 [技能库](skills.md)。 |
| `depends_on`、`depends_on_run` | 等待指定任务或整批任务完成后再派发。 |
| `attachments_from` | 将有权访问的上游任务产物转为本任务附件。 |
| `output_limit` | 输出截断上限，默认 200000 字节。 |

派发会检查节点权限、模型、技能及命令策略。权限或审核拒绝可能返回 403，并留下一条 `denied` 任务；`pending_review` 表示待人工审核，不会进入执行队列。

## 查询、日志与取消

```bash
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/tasks/t-xxxxx"
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks/t-xxxxx/logs?after_id=0"
curl -sS -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks/t-xxxxx:cancel"
```

- `GET /tasks/{id}` 返回 `task`；`queued → assigned → working → completed/failed`。`timed_out`、`cancelled`、`denied` 也为终态。轮询间隔建议 2–3 秒。
- 日志响应包含 `logs` 和 `next_id`；后续请求使用 `?after_id=<next_id>`。终态读取 `task.result`，包括 `summary`、`exit_code`、输出尾部、`session_id`、`artifacts`。
- `pending_review` 等待人工通过、拒绝或超时；向用户报告待审核状态，停止密集轮询。取消返回 `accepted=false` 表示任务已终止。执行中的取消由探针异步处理。

## 检索与审计

```bash
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks?status=failed&limit=50&offset=0"
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks/t-xxxxx/events"
curl -sS -X POST "$AGENT_MESH_BASE_URL/tasks:batch_delete" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"task_ids":["t-xxxxx"]}'
```

任务列表支持 `agent_id`、`status`、`mode`、`run_id`、`search`、`started_after`、`started_before`、`limit`、`offset`。`events` 记录派发、取消和审核等操作。删除任务及其产物前需确认用户意图。
