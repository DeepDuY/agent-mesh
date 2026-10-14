# 定时任务

服务端按五段 cron（分、时、日、月、周）派发 `command` 或 `llm` 任务。计划可指定 `timezone`（IANA 名）；省略时使用全局设置，再回退服务器时区。错过的时刻不补跑；上一轮仍活动时跳过本轮。

## 创建

```bash
set -a; . ./.env; set +a
curl -sS -X POST "$AGENT_MESH_BASE_URL/schedules" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"nightly-check","cron":"0 3 * * *","timezone":"Asia/Shanghai","agent_id":"3","mode":"command","instruction":"df -h"}'
# => {"schedule":{"id":5,"next_run_at":"...","enabled":true,...}}
```

`name` 在系统内唯一；节点必须存在且调用者有权访问。`timezone`、`enabled`、`timeout_s`、`model`、`workdir`、`output_limit`、`session_id`、`skills`、`attachments` 为可选字段。cron 支持 `*`、范围、列表、步长；周字段 `0/7` 均表示周日，日与周同时限定时按“或”处理。

## 查询与维护

```bash
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/schedules"
curl -sS -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/schedules/5:run"
curl -sS -X PATCH "$AGENT_MESH_BASE_URL/schedules/5" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled":false}'
```

`GET/PATCH/DELETE /schedules/{id}` 用于详情、编辑、删除；`:run` 立即执行一次，不改变下次计划时间。响应的 `next_run_at`、`last_run_at` 为 UTC 时间；`last_status` 为上一轮派发状态。仅能访问本人或团队可见的计划。
