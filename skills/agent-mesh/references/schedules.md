# 定时任务：按 cron 周期自动派发

当用户要「每隔一段时间自动跑」「每天/每小时做一次」「定时巡检/日报/健康检查」时，用定时任务：由编排器的**进程内调度器**按 cron 表达式到点自动派发任务（command 或 llm），无需重复手动触发。

## 何时用

| 用户诉求 | 用户可能的说法 | 做法 |
|---|---|---|
| 周期执行命令 | 「每小时跑一次健康检查」「每天备份」 | 建 `command` 定时任务 |
| 周期让远程 AI 干活 | 「每天早上让远程机器总结昨天的日志」 | 建 `llm` 定时任务 |
| 立即跑一次 | 「现在就按那个定时任务跑一次」 | `POST /schedules/{id}/run` |
| 暂停/恢复 | 「先别自动跑了」「恢复那个定时」 | `PATCH /schedules/{id}` `{"enabled":false/true}` |

## 调度语义（重要）

- cron 为 **5 段**：`分 时 日 月 周`，支持 `*`、`a`、`a-b`、`a,b`、`*/n`；**周字段 0/7 均为周日**；日/周同时限定时按 Vixie 语义取「或」。
- 时区：任务自身 `timezone` > 全局设置 `schedule_timezone` > **服务器系统时区**。留空即用系统时区。
- **错过不补跑**：服务器停机期间错过的时刻不会在恢复后补跑，只对齐下一个未来时刻。
- **不重叠**：同一定时任务上一轮任务仍在 `queued/assigned/working` 时，本轮跳过（`last_status=skipped`）。
- 非法 cron 会被自动停用并记录原因（`last_status`）。

## 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/schedules` | 新建 |
| GET | `/schedules` | 列表（只看得到自己/本团队的） |
| GET | `/schedules/{id}` | 详情 |
| PATCH | `/schedules/{id}` | 更新（改 cron/时区/启停会重算下次运行） |
| DELETE | `/schedules/{id}` | 删除 |
| POST | `/schedules/{id}/run` | 立即运行一次（不改下次运行时间） |

## 新建

```bash
set -a; . ./.env; set +a
curl -s -X POST "$AGENT_MESH_BASE_URL/schedules" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"nightly-check","cron":"0 3 * * *","agent_id":3,"mode":"command","instruction":"df -h && free -m"}'
# => {"schedule":{...,"next_run_at":"2026-...Z","enabled":true}}
```

字段：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 唯一名称（字母/数字/`. _ -`） |
| `cron` | 是 | 5 段表达式 |
| `agent_id` | 是 | 目标节点（数字 id / device_id / 显示名） |
| `mode` | 是 | `command` / `llm` |
| `instruction` | 是 | 命令或自然语言任务 |
| `timezone` | 否 | IANA 名（如 `Asia/Shanghai`），留空用系统时区 |
| `enabled` | 否 | 默认 true |
| `timeout_s` / `model` / `workdir` / `output_limit` / `session_id` | 否 | 同任务派发参数 |
| `attachments` | 否 | 文件库 file_id 列表（见 [files.md](files.md)） |

新建/更新时会校验：节点存在且你有权访问、cron 合法、llm 模式已配置可用模型。命令模式的权限策略在**每次运行派发时**按服务端策略预检（被拒会生成 `denied` 任务并记录原因）。

## 查看与管理

```bash
# 列表
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/schedules"
# 立即运行一次
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/schedules/5/run"
# 暂停
curl -s -X PATCH -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled":false}' "$AGENT_MESH_BASE_URL/schedules/5"
```

- 定时任务派发的任务，其 `task_events` 里有一条 `scheduled` 事件，含 `schedule_id`/`schedule_name`/`trigger`（`scheduled` 或 `manual`）。
- `next_run_at` / `last_run_at` 为 UTC ISO；`last_status` 为最近一次派发结果（`queued` / `skipped` / `denied` / `error: ...`）。
- 可见性同任务：只能管理自己（或本团队）创建的定时任务，无权的返回 404。
