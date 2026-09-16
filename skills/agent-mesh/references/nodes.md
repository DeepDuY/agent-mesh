# 节点（只读）

用于**挑一台合适的远程机器**，然后去 [tasks.md](tasks.md) 派发任务。本技能只做只读查询；改别名/描述、升级探针、安装/删除节点属于管理面，请让管理员在 Web 控制台操作。

## 何时用

- 用户说「在服务器 A 上跑」「找台机器帮我做 X」「看看有哪些机器在线」「哪台适合跑这个」。
- **派发任务前先看一眼**：确认目标在线、用途描述匹配、资源不紧张。离线节点派发会一直排队。

## 列出可见节点

```bash
set -a; . ./.env; set +a
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents"
```

只返回**你有权限访问的**节点（按团队/用户授权过滤）。关键字段：

| 字段 | 含义 / 怎么用 |
|------|---------------|
| `id` | 数字主键，**推荐用它派发**（唯一、稳定） |
| `device_id` | machine-id，稳定的设备标识 |
| `agent_id` / `alias` | 显示名，可能重复，不要用它作为唯一键 |
| `display_name` | 给人看的名字（`alias`→`hostname`→`agent_id` 依次兜底） |
| `online` | 是否在线（心跳期内）。**离线节点派发会一直排队** |
| `effective_description` | **选节点的核心依据**：节点自身描述优先，未设置时用其绑定模板的「节点描述」 |
| `description` | 节点自身描述（可能为空） |
| `template_name` | 绑定模板的名字（只读展示，说明它的用途/权限来源） |
| `version` | 探针版本 |
| `cpu_percent` / `mem_percent` / `mem_used_mb` / `mem_total_mb` | 资源占用，判断是否适合跑重任务 |
| `current_task_id` | 当前任务（多任务并发时仅供参考） |
| `ip_address` | 节点来源 IP（服务端按连接推导） |

## 单节点 / 节点详情

```bash
# 单个节点（agent_id 可传数字 id / device_id / 显示名）
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents/3"
# 节点详情 + 最近 20 条任务
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents/3/detail"
```

`GET /agents/{id}/detail` 在节点信息之外附带 `tasks`（该节点最近任务），适合回答「这台机器最近跑了什么」。非管理员看不到 `access`（ACL）字段。

## 怎么挑

1. `online=true` 优先。
2. `effective_description` 与需求最匹配的（例如「生产 Web 服务器，只跑部署类命令」）。
3. 对比 `cpu_percent`/`mem_percent`，避开负载高的。
4. 记住它的数字 `id`，到 [tasks.md](tasks.md) 派发。
