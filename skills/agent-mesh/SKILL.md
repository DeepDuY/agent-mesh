---
name: agent-mesh
description: 通过 agent-mesh 编排器把命令/自然语言任务派发到远程边缘节点、监控执行、取回产物，并查看节点、使用文件库与技能库。当用户要求「在某台远程机器上执行/运行/部署/测试」「查看节点状态」「把文件传到远程处理」「把远程产出拿回来」时使用本技能。
---

# agent-mesh 编排器控制

agent-mesh 让你把任务委派给远程边缘节点（edge agent）执行，并统一监控、取回结果。本 SKILL 面向**主 Agent**：目标是让主 Agent 读完就知道**什么需求该调用哪个接口**。

> 本技能只覆盖**主 Agent 有权限使用**的能力。模板/权限、全局配置、用户管理等属于**管理面**，主 Agent 一律不可调用（相关接口对 API 不开放），不在此文档内。

## 0. 能力总览：什么需求用什么

| 用户诉求 | 用什么 | 详见 |
|----------|--------|------|
| 在远程机器跑命令/脚本/测试 | `POST /tasks/dispatch`（`mode=command`） | §4 |
| 让远程机器上的 LLM 完成自然语言任务 | `POST /tasks/dispatch`（`mode=llm`） | §4 |
| **把本地文件交给远程任务处理** | 先 `POST /files` 上传，再派发带 `attachments` | §5 |
| **取回任务产生的文件/产物** | `task.result.artifacts` → `GET /artifacts/{task_id}/{artifact_id}` | §5 |
| 看远程任务跑到哪了 / 实时输出 | `GET /tasks/{id}/status`、`GET /tasks/{id}/logs` | §6 |
| 停止正在跑的任务 | `POST /tasks/{id}/cancel` | §6 |
| 查看/挑选节点（哪台在线、干啥的） | `GET /agents`（看 `online`/`effective_description`/`template_name`） | §3 |
| 看节点详情 / 最近任务 | `GET /agents/{id}/detail` | §3 |
| 给节点改备注名 / 写描述 | `PATCH /agents/{id}/alias`、`PATCH /agents/{id}/description` | §8 |
| 让远程 LLM 按需使用某个专业技能 | 技能库（上传/浏览），边沿自主取用 | §7 |
| 请求升级节点探针 | `POST /agents/{id}/upgrade` | §8 |
| 装一台新机器 / 卸载节点 | `GET /bootstrap/install.sh` / `DELETE /agents/{id}` | §9 |

**一句话判断**：任务本身要「在别处执行」→ §4 派发；任务要用到**文件**→ §5；只是查询/管理→ §3/§6/§8。

---

## 1. 认证：token 会过期（务必先读）

控制接口（REST/MCP）需要**用户 token**。

| 类型 | 来源 | 有效期 |
|------|------|--------|
| **session token** | `POST /auth/login`（账号+密码） | **默认 24 小时** |
| **API token** | 由管理员创建/下发 | 长期有效 |

登录：

```bash
curl -s -X POST http://<host>:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<用户名>","password":"<密码>"}'
# => {"username":"...","token":"<token>","role":"...","token_type":"session"}
```

之后所有请求带 `Authorization: Bearer <token>`。

### ⚠️ token 过期怎么处理（重要）

session token 24 小时后失效。**任何请求返回 `401 Unauthorized` 就是 token 过期或失效**：

1. **不要**反复重试或尝试绕过。
2. **明确向用户索取账号密码**（例如：「编排器 token 已过期，请提供用户名和密码，我重新登录」）。不要猜测密码，也不要把密码写进日志/仓库。
3. 重新 `POST /auth/login` 拿到新 token，替换后继续。
4. 想免去反复登录，可请管理员给一个**长期 API token**。

> 建议把「登录」封装成函数，遇 401 自动重新登录；若没有凭证就停下来问用户。

## 2. 地址

- **REST Base URL**：`http://<host>:8000/api`

不知道地址就问用户；配置页的「公开地址」即 REST 地址。

## 3. 选节点（先看再派发）

`GET /agents` 列出**你有权访问的**节点（按团队/用户授权过滤）。**派发前先看 `online` 和 `effective_description`**：

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/agents
```

关键字段：

- `id`：数字主键，**推荐用这个派发**（唯一）。
- `device_id`：machine-id，稳定设备标识；`agent_id`/`alias`：显示名，可能重复。
- `online`：是否在线（心跳期内）。**离线节点派发会一直排队**。
- `effective_description`：**选节点的依据**——节点自身描述优先，未设置时用其绑定模板的「节点描述」。
- `description`：节点自身的描述（可能为空）。
- `template_name`：绑定模板的**名字**（只读展示）。
- `version`：探针版本；`cpu_percent`/`mem_percent`：资源占用。
- `current_task_id`：当前任务（多任务并发时仅供参考）。

节点详情（含最近 20 条任务）：`GET /agents/{id}/detail`（`id` 可传数字 id / device_id / 显示名）。

## 4. 派发任务（核心）

`POST /tasks/dispatch`：

```bash
curl -s -X POST http://<host>:8000/api/tasks/dispatch \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"command","instruction":"echo hello && uname -a","timeout_s":120}'
# => {"task_id":"t-xxxxx","status":"queued"}
```

### 选 `command` 还是 `llm`？

- **`mode=command`**：`instruction` 就是一条 shell 命令（`bash -c` 原样执行），**不经过 LLM**。适合明确、确定性的操作（部署、跑测试、看系统信息、文件操作）。工作目录下**新增的文件会自动作为产物**收集。
  - **受节点权限约束**：节点若绑定只读/plan 权限，危险或非白名单命令会被**拒绝**（返回 403 或任务失败，摘要含「权限被拒绝」）。遇到被拒，向用户说明是权限策略，**不要尝试改权限/换模板**（那是管理员在页面做的事）。
- **`mode=llm`**：`instruction` 是**自然语言任务**，交给节点本机的 opencode 处理。适合需要推理/多步/写代码的模糊任务。需要节点已配置可用模型；只收集 LLM 主动声明的产物。

> 能用明确命令做的事，优先 `command`（更快、更可控、更省 token）；需要「动脑」才用 `llm`。

### 常用可选参数

| 参数 | 说明 |
|------|------|
| `workdir` | 工作目录。**不填**则自动落在独立子目录 `<EDGE_WORKDIR>/tasks/<task_id>/`（推荐，互不干扰） |
| `timeout_s` | 超时秒数，默认 300；超时任务置 `timed_out` |
| `model` | 仅 llm：指定模型，须命中可用模型列表；不填用节点/模板/全局默认 |
| `max_retries` | 超时后自动重试次数 |
| `depends_on` | 依赖的任务 id 列表（当前仅校验存在，不阻塞执行） |
| `session_id` | 仅 llm：续用上一个 llm 任务的会话 |
| `attachments` | **文件库 file_id 列表**，见 §5 |
| `skills` | 提示该任务可参考的技能名（预留） |
| `output_limit` | 输出截断字节数，默认 200000 |

**续用 LLM 会话**：先读上一任务 `result.session_id`，派发时带上 `session_id`；本次返回的 `result.session_id` 为实际会话 ID。

## 5. 文件模块：任务输入与产物的搬运

解决「主 Agent 所在机器」与「边缘节点工作目录」之间的文件搬运。

### 5.1 何时用
- **任务需要读取/处理某个本地文件** → 走「上传 + `attachments`」。
- **任务产出了文件，用户想拿回来** → 走「产物下载」。

### 5.2 何时**不**用
- 只是一小段文本 → 直接写进 `instruction`。
- command 模式产生的文件 → 已自动作为 task 产物收集，无需手动上传文件库。

### 5.3 上传本地文件给任务用

```bash
# 1) 上传（可多文件；返回 file_id + md5，同内容自动去重）
curl -s -X POST http://<host>:8000/api/files \
  -H "Authorization: Bearer <token>" \
  -F "files=@./data.csv" -F "files=@./config.yaml"
# => {"files":[{"file_id":"f-xxxx","filename":"data.csv","size":123,
#              "content_type":"text/csv","md5":"...","download_url":"/api/files/f-xxxx"}]}

# 2) 派发时引用 file_id
curl -s -X POST http://<host>:8000/api/tasks/dispatch \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"llm","instruction":"分析工作目录下的 data.csv，输出 summary.md",
      "attachments":["f-xxxx","f-yyyy"]}'
```

- 附件以**原文件名**写入任务工作目录；探针下载后**按 md5 校验**，不一致则任务失败（`summary: attachment download failed`）。
- `mode=command` 也可用 `attachments`：文件先落到工作目录，命令直接引用文件名即可。

### 5.4 取回任务产物

```bash
# 任务详情里有 result.artifacts（artifact_id / filename / size）
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx
# 下载单个产物
curl -s -H "Authorization: Bearer <token>" \
  http://<host>:8000/api/artifacts/t-xxxxx/a-1 -o summary.md
```

- **command 模式**：工作目录下新增文件自动进产物。
- **llm 模式**：只收集 LLM 在最终 JSON 里声明的文件；大文件/多文件要求打包成 `.tar.gz`/`.zip`。

### 5.5 文件库管理

```bash
curl -s -H "Authorization: Bearer <token>" "http://<host>:8000/api/files?search=csv"   # 列表/搜索
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/files/f-xxxx -o f     # 下载
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"file_ids":["f-xxxx"]}' http://<host>:8000/api/files/batch-delete
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"file_ids":["f-xxxx","f-yyyy"]}' http://<host>:8000/api/files/batch-download -o files.zip
```

- 文件库按内容去重；删除不做引用校验，被删文件的任务执行时会因下载失败而失败。

## 6. 监控、取消与审计

### 6.1 任务状态
状态机：`queued → assigned → working → completed / failed / timed_out`，另有 `cancelled`。

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx/status
```

终态后读 `task.result`（`summary`/`exit_code`/`stdout_tail`/`stderr_tail`/`artifacts`/`session_id`）。轮询间隔建议 2–3 秒。

### 6.2 实时输出

```bash
curl -s -H "Authorization: Bearer <token>" \
  "http://<host>:8000/api/tasks/t-xxxxx/logs?after_id=0"
# => {"task_id":"...","logs":[{"id":1,"kind":"text","content":"..."}],"next_id":2}
# 下次用 after_id=next_id 增量拉取
```

`kind`：`text`（LLM 文本）/`error`/`complete`/`raw`（command 原始行）。

### 6.3 取消

```bash
curl -s -X POST -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx/cancel
```

- `queued`：移出队列，不执行。
- `assigned`/`working`：edge 约 3 秒内杀掉整个进程组（SIGTERM→SIGKILL），不提交结果。
- 已终态：返回 `{"accepted": false}`（幂等）。

### 6.4 查找 / 清理 / 审计

```bash
# 列表（分页+筛选）
curl -s -H "Authorization: Bearer <token>" \
  "http://<host>:8000/api/tasks?status=working&mode=llm&search=hello&limit=50"
# 批量删除（按 id 或按条件）
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"all_matching":true,"status":"cancelled"}' http://<host>:8000/api/tasks/batch-delete
# 审计事件（dispatched / cancelled / permission_denied）
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx/events
```

- `search` 匹配指令/任务 id/节点；`started_after`/`started_before` 为 ISO 时间。

## 7. 技能库（给远程 LLM 用的专业知识）

技能是打包好的 `SKILL.md` 指南，供**边缘节点的 LLM 在任务中自主取用**。

- **何时用**：希望远程 llm 任务按某个专业流程/规范工作时。通常**只需在 `instruction` 里提示**，边沿会自行浏览并下载。
- **何时不用**：任务简单、无需额外知识时。

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/skills          # 摘要
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/skills/<name>/download -o skill.zip
```

管理（Web「技能」页或 REST）：`POST /api/skills`（上传 zip，需含带 `name`/`description` frontmatter 的 `SKILL.md`；重传同名 version+1）、`PATCH /api/skills/{name}`、`DELETE /api/skills/{name}`。

**下载本 SKILL 的个性化版本**：`GET /api/skill-doc/agent-mesh` 返回**已填好地址与你的 token** 的本文档。

## 8. 节点操作

```bash
# 别名（null 清除）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"alias":"生产节点A"}' http://<host>:8000/api/agents/3/alias
# 节点自身描述（作为 effective_description 的首选；null 清除）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"description":"生产 Web 服务器，只跑部署类命令"}' http://<host>:8000/api/agents/3/description
# 请求升级探针（空闲时自动下载新版并重启，失败自动回滚）
curl -s -X POST -H "Authorization: Bearer <token>" http://<host>:8000/api/agents/3/upgrade
# 删除节点（**破坏性**：会向该机器下发卸载）
curl -s -X DELETE -H "Authorization: Bearer <token>" http://<host>:8000/api/agents/3
```

## 9. 安装节点

安装（需**长期有效**的用户 API token）：

```bash
TOKEN='<长期 token>' bash <(curl -fsSL -H "Authorization: Bearer $TOKEN" \
  http://<host>:8000/api/bootstrap/install.sh)
```

- 目标机自动按 OS/ARCH 下载探针、注册开机自启并启动，以 device_id 注册为节点。
- 删除节点会卸载远端 agent，谨慎操作（见 §8）。

## 10. 完整流程（推荐节奏）

1. `GET /agents` → 选 `online=true` 且 `effective_description` 匹配需求的节点，记下数字 `id`。
2. 若要处理本地文件：`POST /files` 上传拿 `file_id`（§5）。
3. `POST /tasks/dispatch` → 拿 `task_id`。
4. 需要进度：`GET /tasks/{task_id}/logs?after_id=<next_id>` 增量看输出。
5. 每 2–3 秒 `GET /tasks/{task_id}/status` 直到终态；用户要停就 `POST /tasks/{task_id}/cancel`。
6. 读 `task.result`；有 `artifacts` 就下载给用户（§5.4）。

## 11. 故障速查

| 现象 | 原因 / 处理 |
|------|-------------|
| `401 Unauthorized` | **token 过期/失效** → 向用户索取账号密码，重新 `POST /auth/login`（§1） |
| 派发 `403` / 任务摘要含「权限被拒绝」 | 节点权限策略拒绝该命令 → 如实说明；不要尝试改权限/换模板 |
| 任务一直 `queued` | 目标节点离线或已达并发上限 → 换在线节点 / 等待 |
| `no LLM model configured` | 节点无可用模型 → 让管理员在管理平台配置 |
| `attachment download failed` | 文件库文件被删或 md5 不符 → 重新上传（§5） |

## 12. 安全

- **可见性按身份隔离**：你只能看到/操作被授权的节点，以及自己（或本团队）的任务与文件；无权访问的资源返回 404（如同不存在）。若需要访问某节点/数据，请让管理员在管理平台授权，**不要尝试绕过**。
- 保管 token；生产用 HTTPS。
- LLM API key 仅存于服务端 DB 与节点 `edge.env`，经心跳下发；**不要写进代码/仓库/日志**。
- 删除节点会卸载远端 agent；取消任务会杀掉进程组——都是破坏性操作，执行前和用户确认。
- **不要调用任何管理面接口**（模板/权限、全局配置、用户管理等）；它们对 API 不开放，调用会失败。

## 参考

- 可运行示例：`references/example-poll.py`（登录→列节点→派发 command→轮询→下载产物）。
