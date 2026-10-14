---
name: agent-mesh
description: 通过 agent-mesh 在远程节点执行命令或 LLM 任务，批量派发、定时运行、传输文件并查询结果。用户要求在其他机器上部署、测试、巡检、处理文件或查看远程节点和任务时使用。
---

# agent-mesh 编排器控制

本技能供主 Agent 通过 REST 操作远程节点。节点配置、用户管理、模板与权限设置由 Web 管理平台处理。

## 连接

- API 地址：`http://<orchestrator-host>:8000/api`。下载的技能包在根目录 `.env` 中提供 `AGENT_MESH_BASE_URL` 与用户 `AGENT_MESH_TOKEN`；地址为空时先向用户获取。
- 先在技能包根目录加载变量，再执行参考文档中的命令；示例保留完整认证头：

```bash
set -a; . ./.env; set +a
```

- 登录以外的请求使用 `Authorization: Bearer $AGENT_MESH_TOKEN`。凭据和 401 处理见 [认证](references/auth.md)。

## 按需求查阅

| 需求 | 参考文档 |
|---|---|
| 查询、选择远程节点 | [节点](references/nodes.md) |
| 单节点命令或 LLM 任务；监控、取消、审计 | [任务](references/tasks.md) |
| 多节点派发、整批监控、重试与汇总 | [批次](references/runs.md) |
| 定时执行 | [定时任务](references/schedules.md) |
| 上传附件、下载任务产物、管理文件库 | [文件](references/files.md) |
| 查询或指定远程 LLM 技能 | [技能库](references/skills.md) |
| 401/403、排队、审核和附件故障 | [故障处理](references/troubleshooting.md) |

## 基本调用顺序

1. `GET /agents`，选择有权使用的节点，优先使用数字 `id`。
2. `POST /tasks`（单节点）或 `POST /runs`（多节点）；需要本地文件时先 `POST /files`。
3. `GET /tasks/{task_id}` 或 `GET /runs/{run_id}` 查询进度；任务日志通过 `/tasks/{task_id}/logs?after_id=...` 增量读取。
4. 终态后检查 `task.result` 并按 `artifacts[].download_url` 下载产物。`pending_review` 等待人工处理，`denied` 表示任务已拦截。
