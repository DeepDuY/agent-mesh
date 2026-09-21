---
name: agent-mesh
description: 通过 agent-mesh 编排器把命令/脚本/自然语言任务派发到远程边缘节点执行，并把文件送过去、把产物取回来；也可查看远程节点状态与资源、查看/取消/审计远程任务、浏览与下载技能。当用户说「在某台远程机器/服务器上执行/运行/部署/安装/测试/排查」「把文件传到远程处理再拿回结果」「把任务丢给远程的 AI/模型跑」「看看有哪些远程机器在线、配置如何」「停止/查看那个远程任务」时使用本技能。
---

# agent-mesh 编排器控制（主 Agent 使用指南）

agent-mesh 让**你（主 Agent）**把工作委派给远程**边缘节点**执行：在别的机器上跑命令、让远程的 LLM 完成自然语言任务、搬运文件、取回产物，并查看节点与任务状态。

本页是**索引**：先看下面的「场景速查」找到该用哪个能力，再进入 `references/` 对应文档看细节。

> 本技能只覆盖**主 Agent 可用的能力**。模板/权限、全局配置、用户管理，以及**节点的编辑类操作**（改别名/描述、升级探针、安装/删除节点）属于管理面，本技能不含，请让管理员在 Web 控制台操作。

## 两种「技能」，别混淆

- **本 agent-mesh 技能包（你正在读的）**：主 Agent 的操作指南，由用户从 Web「个人中心」下载，**不在编排器里**。
- **远程技能库（编排器上的）**：管理员上传、可动态增删的专业技能（`SKILL.md`），供**远程节点的 LLM** 在 `llm` 任务中按需使用。你可以 `GET /skills` 查看可用（`enabled`）名单；要让任务用某技能，在 `instruction` 里点名即可（细节见 [references/skills.md](references/skills.md)）。

## 场景速查：用户这么说 → 你要做这件事 → 细节在

| 用户的话（举例） | 你能达成的结果 | 看这里 |
|---|---|---|
| 「在服务器 A 上执行 `docker ps`」「跑一下这个脚本」「部署到那台机器」 | 在指定远程节点上跑一条 shell 命令，拿到 stdout/exit code | [references/tasks.md](references/tasks.md) |
| 「让远程那台机器上的 AI 帮我改代码 / 排查日志 / 分析数据」 | 把自然语言任务交给远程节点的 opencode 执行（多步推理、写文件） | [references/tasks.md](references/tasks.md) |
| 「把这个文件传到远程处理一下」「用那个 csv 生成报表」 | 上传本地文件到文件库，再作为附件随任务下发到节点工作目录 | [references/files.md](references/files.md) |
| 「把远程生成的文件/产物拿回来」 | 从任务结果里下载产物到本地 | [references/files.md](references/files.md) |
| 「看看有哪些机器在线」「哪台适合跑这个任务」 | 列出你有权限的节点，看在线状态、用途描述、资源占用、版本 | [references/nodes.md](references/nodes.md) |
| 「那台机器最近跑了什么」 | 查看节点详情与最近任务 | [references/nodes.md](references/nodes.md) |
| 「任务跑到哪了」「把输出实时给我看」 | 轮询任务状态、增量拉取实时日志 | [references/tasks.md](references/tasks.md) |
| 「把那个任务停了」 | 取消排队中/执行中的任务（杀掉远程进程组） | [references/tasks.md](references/tasks.md) |
| 「查一下之前那个任务」「看看谁在什么时候改过」 | 任务列表/搜索/审计事件 | [references/tasks.md](references/tasks.md) |
| 「每天/每小时自动跑一次」「定时巡检/日报」 | 建 cron 定时任务，到点自动派发（命令或 LLM） | [references/schedules.md](references/schedules.md) |
| 「按某规范/流程让远程 LLM 干活」「用那个技能」 | 先 `GET /skills` 看可用技能，再在 `llm` 任务的 `instruction` 里点名 | [references/skills.md](references/skills.md) |
| 「报错了 / 401 / 一直排队」 | 故障定位与处理 | [references/troubleshooting.md](references/troubleshooting.md) |

**一句话判断**：任务要「在别处执行」→ 派发任务（tasks）；任务要用到**文件** → 文件库（files）；只是**查询**节点 → nodes；要看任务进度/停止 → tasks。

## 连接与认证（先读）

- **REST Base URL**：`http://<orchestrator-host>:8000/api`（本包已尽量填好；未填则向用户索取「公开地址」）。
- **用户 token 与地址**：本技能目录下的 **`.env`** 已写入：
  - `AGENT_MESH_BASE_URL`：REST 地址
  - `AGENT_MESH_TOKEN`：你的用户 token

执行任何示例前，先加载环境变量：

```bash
set -a; . ./.env; set +a      # 在本 SKILL 目录下执行
# 之后请求用：-H "Authorization: Bearer $AGENT_MESH_TOKEN"
```

**token 会过期**：401 表示 token 过期/失效。**不要反复重试**，向用户说明并请其在 Web 控制台「个人中心」重新生成 token 并更新 `.env`。细节与安全须知见 [references/auth.md](references/auth.md)。

## 推荐工作节奏

1. `GET /agents` → 选一个 `online=true` 且 `effective_description` 匹配需求的节点，记下数字 `id`（见 [references/nodes.md](references/nodes.md)）。
2. 若任务要用本地文件：`POST /files` 上传拿 `file_id`（见 [references/files.md](references/files.md)）。
3. `POST /tasks/dispatch` → 拿到 `task_id`（见 [references/tasks.md](references/tasks.md)）。
4. 需要进度：`GET /tasks/{task_id}/logs?after_id=<next_id>` 增量看输出。
5. 每 2–3 秒 `GET /tasks/{task_id}/status` 直到终态；用户要停就 `POST /tasks/{task_id}/cancel`。
6. 读 `task.result`；有 `artifacts` 就下载给用户。

## 参考文档

- [references/auth.md](references/auth.md) — 地址、token、401 处理、安全
- [references/nodes.md](references/nodes.md) — 选节点、节点详情（只读）
- [references/tasks.md](references/tasks.md) — 派发、监控、取消、审计、完整流程
- [references/schedules.md](references/schedules.md) — 定时任务（cron 周期派发）
- [references/files.md](references/files.md) — 上传附件、下载产物、文件库管理
- [references/skills.md](references/skills.md) — 技能浏览与下载
- [references/troubleshooting.md](references/troubleshooting.md) — 故障速查
