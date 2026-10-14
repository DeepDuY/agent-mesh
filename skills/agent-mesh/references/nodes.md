# 节点查询与选择

任务派发前查询用户可访问的节点：

```bash
set -a; . ./.env; set +a
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents"
```

| 字段 | 用途 |
|---|---|
| `id` | 数字主键；推荐作为任务的 `agent_id`。 |
| `online` | 在线状态；离线节点的任务会留在队列。 |
| `effective_description` | 节点自身描述优先，否则使用模板的节点描述；用于匹配任务要求。 |
| `os`、`arch`、`runtime`、`version` | 运行环境与探针版本。 |
| `cpu_percent`、`mem_percent` | 当前资源指标，可能缺失或滞后。 |
| `device_id` | 稳定设备标识；也可用于派发。 |
| `alias`、`hostname`、`agent_id` | 显示/注册信息；同名节点可能存在，应优先使用数字 `id` 或 `device_id`。 |

查询单节点及其当前用户可见的最近任务：

```bash
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents/3"
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents/3?include=tasks"
```

选点时确认在线状态、用途与资源指标；无可访问节点时请求管理员授权。节点的修改操作由 Web 管理平台处理。
