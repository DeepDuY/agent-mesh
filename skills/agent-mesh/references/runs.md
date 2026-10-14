# 批次任务（Run）

`POST /runs` 将同一条指令派发给多个节点，返回 `run_id`、已排队任务 ID、待审核任务和拒绝目标。目标逐个校验；HTTP 200 不代表所有目标成功入队。

```bash
set -a; . ./.env; set +a
curl -sS -X POST "$AGENT_MESH_BASE_URL/runs" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -H "Idempotency-Key: inspection-2026-10-14" \
  -d '{"targets":[3,7],"mode":"command","instruction":"df -h","timeout_s":120}'
# => {"run_id":"r-...","queued":2,"task_ids":["t-...","t-..."],"pending_review":[],"denied":[]}
```

`targets` 为节点引用数组；可用的任务参数见 [单节点任务](tasks.md)。查看 `queued`、`pending_review`、`denied` 以确认逐节点结果；目标数量受服务端扇出限制。同一用户使用相同 `Idempotency-Key` 的顺序重试会返回原批次，不要为不同任务复用键；并发提交不保证原子去重。

## 监控与批次操作

```bash
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/runs/r-xxxxx?wait=30"
curl -sS -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/runs/r-xxxxx:retry"
curl -sS -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/runs/r-xxxxx:cancel"
```

- `GET /runs/{id}` 返回 `counts`、`active` 与分页的任务摘要；`?status=failed` 仅返回失败任务摘要，`?include_tasks=false` 只返回计数。`wait` 最长 30 秒，仅在批次有活动任务时长轮询。
- `:retry` 重新排队批内 `failed`/`timed_out` 任务；因依赖失败而取消的下游任务不在此操作范围。`:cancel` 取消批内非终态任务。

## 上游依赖与产物

在单节点汇总任务中指定 `depends_on_run: "r-xxxxx"`，等待批次内全部任务完成。需要使用上游产物时，再提供上游 `task_id` 的 `attachments_from`。任何依赖任务失败时，汇总任务会被取消；产物转附件要求调用者有权访问上游任务。
