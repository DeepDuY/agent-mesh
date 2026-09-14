---
name: agent-mesh
description: 通过 agent-mesh 编排器把命令/自然语言任务派发到远程边缘节点、监控执行、取回产物，并管理节点、模板、权限、文件库与技能库。当用户要求「在某台远程机器上执行/运行/部署/测试」「查看节点状态」「把文件传到远程并处理」「把远程产出拿回来」「管理远程 agent 集群」时使用本技能。
---

# agent-mesh 编排器控制

agent-mesh 让你把任务委派给远程边缘节点（edge agent）执行，并统一监控、管理。本 SKILL 面向**主 Agent**：目标是让主 Agent 在读完后**知道什么需求该调用哪个接口**，而不是死记端点。

## 0. 能力总览：什么需求用什么

| 用户诉求 | 用什么 | 详见 |
|----------|--------|------|
| 在远程机器跑命令/脚本/测试 | `POST /tasks/dispatch`（`mode=command`） | §4 |
| 让远程机器上的 LLM 完成自然语言任务 | `POST /tasks/dispatch`（`mode=llm`） | §4 |
| **把本地文件交给远程任务处理** | 先 `POST /files` 上传，再派发带 `attachments` | §5 |
| **取回任务产生的文件/产物** | `task.result.artifacts` → `GET /artifacts/{task_id}/{artifact_id}` | §5 |
| 看远程任务跑到哪了 / 实时输出 | `GET /tasks/{id}/status`、`GET /tasks/{id}/logs` | §6 |
| 停止正在跑的任务 | `POST /tasks/{id}/cancel` | §6 |
| 查看/挑选节点（哪台在线、干啥的） | `GET /agents`（看 `description`/`online`） | §3 |
| 给一类节点统一配置（模型/提示词/权限） | 模板 `POST /templates` + `PATCH /agents/{id}/template` | §8 |
| 单独调某个节点的 LLM / 提示词 | `PATCH /agents/{id}/llm_config` / `system_prompt` | §9 |
| 让远程 LLM 按需使用某个专业技能 | 技能库 `POST /skills`（上传）、边沿自主取用 | §7 |
| 装一台新机器 / 卸载节点 | `GET /bootstrap/install.sh` / `DELETE /agents/{id}` | §11 |
| 改全局模型/公开地址/并发/默认权限 | `PATCH /settings` | §10 |

**一句话判断**：任务本身要「在别处执行」→ §4 派发；任务要用到**文件**→ §5；只是查询/管理→ §3/§6/§9。

---

## 1. 认证：token 会过期（务必先读）

编排器的控制接口（REST/MCP）需要**用户 token**。注意全局 token（`AGENT_MESH_TOKEN`）**只能**给边缘节点做心跳/上报，**不能**用于控制接口。

有两种用户 token：

| 类型 | 来源 | 有效期 | 用途 |
|------|------|--------|------|
| **session token** | `POST /auth/login`（账号+密码） | **默认 24 小时** | 交互式使用；推荐主 Agent 使用 |
| **API token** | 创建用户时一次性下发，或 admin 轮换 | 长期有效 | 长期脚本/自动化 |

登录：

```bash
curl -s -X POST http://<host>:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<用户名>","password":"<密码>"}'
# => {"username":"...","token":"<token>","role":"admin","token_type":"session"}
```

之后所有请求带 `Authorization: Bearer <token>`。

### ⚠️ token 过期怎么处理（重要）

session token 24 小时后失效。**任何请求返回 `401 Unauthorized`（或 `{"detail":"..."}` 表示未认证）时，就是 token 过期或失效**。此时：

1. **不要**反复重试或尝试绕过。
2. **明确向用户索取账号密码**（例如：「编排器 token 已过期，请提供用户名和密码，我重新登录」）。不要猜测密码，也不要把密码写进日志/仓库。
3. 重新 `POST /auth/login` 拿到新 token，替换后继续。
4. 如果用户希望免去反复登录，可让 admin 用 `POST /auth/users` 创建用户拿**长期 API token**，或 `POST /auth/users/{username}/token` 轮换获得长期 token。

> 主 Agent 建议：把「登录」封装成一个函数，遇到 401 自动重新登录；若登录也需要凭证且本地没有，就停下来问用户。

## 2. 地址

- **REST Base URL**：`http://<host>:8000/api`
- **MCP（SSE）**：`http://<orchestrator-host>:8001/`

如果不知道地址，向用户询问；配置页的「公开地址」即为 REST 地址。

## 3. 选节点（先看再派发）

`GET /agents` 列出全部节点。**派发前先看 `online` 和 `description`**：

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/agents
```

关键字段：

- `id`：数字主键，**推荐用这个派发**（唯一）。
- `device_id`：machine-id，稳定设备标识；`agent_id`/`alias`：显示名，可能重复。
- `online`：是否在线（心跳期内）。**离线节点派发会一直排队**。
- `description`：运维写的用途说明（如「生产 Web 服务器」）——**据此选对节点**。
- `template_id`：绑定的模板（决定模型/提示词/权限）。
- `version`：探针版本；`cpu_percent`/`mem_percent`：资源占用。
- `current_task_id`：当前任务（多任务并发时仅供参考）。

引用节点时，`agent_id` 参数可传数字 `id`、`device_id` 或显示名——但**优先用数字 `id`**，最不易混淆。

## 4. 派发任务（核心）

`POST /tasks/dispatch`：

```bash
curl -s -X POST http://<host>:8000/api/tasks/dispatch \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"command","instruction":"echo hello && uname -a","timeout_s":120}'
# => {"task_id":"t-xxxxx","status":"queued"}
```

### 选 `command` 还是 `llm`？

- **`mode=command`**：`instruction` 就是一条 shell 命令（`bash -c` 原样执行），**不经过 LLM**。适合明确、确定性的操作（部署、跑测试、看系统信息、文件操作）。
  - 工作目录下**新增的文件会自动作为产物**收集。
  - **受节点权限约束**：节点若绑定只读/plan 权限，危险或非白名单命令会被**拒绝**（返回 403 或任务失败，摘要含「权限被拒绝」）。遇到被拒，向用户说明是权限策略，而不是命令本身有错。
- **`mode=llm`**：`instruction` 是**自然语言任务**，交给节点本机的 opencode 处理。适合需要推理/多步/写代码的模糊任务。
  - 需要节点已配置可用模型，否则被拒。
  - 只会收集 LLM 主动声明的产物。

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

**续用 LLM 会话**：给需要上下文的连续任务，先读上一任务 `result.session_id`，派发时带上 `session_id`；返回的 `result.session_id` 是本次实际会话。

派发后轮询直到终态（见 §6）。

## 5. 文件模块：任务输入与产物的搬运

文件模块解决一件事：**在「主 Agent 所在机器」和「边缘节点工作目录」之间搬运文件**。

### 5.1 何时用

- **任务需要读取/处理某个本地文件**（配置、数据、脚本、压缩包）→ 走「上传 + `attachments`」。
- **任务产出了文件，用户想拿回来**（报告、构建产物、日志、打包结果）→ 走「产物下载」。

### 5.2 何时**不**用

- 只是一小段文本内容 → 直接写进 `instruction`，不必上传。
- command 模式产生的文件 → 已自动作为 **task 产物**收集，**不需要**手动上传文件库（见下）。

### 5.3 上传本地文件给任务用

```bash
# 1) 上传（可多文件；服务端返回 file_id + md5，同内容自动去重）
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

- 附件以**原文件名**写入任务工作目录；探针下载后会**按 md5 校验**，不一致则任务直接失败（`summary: attachment download failed`）。
- `mode=command` 也可以用 `attachments`：文件先落到工作目录，命令即可直接引用文件名。

### 5.4 取回任务产物

任务完成后，`task.result.artifacts` 是产物列表（`artifact_id`/`filename`/`size`）：

```bash
# 先看任务详情的产物列表
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx
# result.artifacts: [{"artifact_id":"a-1","filename":"summary.md","size":123,"download_url":...}, ...]

# 下载单个产物
curl -s -H "Authorization: Bearer <token>" \
  http://<host>:8000/api/artifacts/t-xxxxx/a-1 -o summary.md
```

- **command 模式**：工作目录下新增的文件自动进产物。
- **llm 模式**：只收集 LLM 在最终 JSON 里 `declared_artifacts` 声明的文件。
- 大文件/多文件：LLM 被要求打包成 `.tar.gz`/`.zip` 再声明。

### 5.5 文件库管理

```bash
curl -s -H "Authorization: Bearer <token>" "http://<host>:8000/api/files?search=csv"   # 列表/搜索
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/files/f-xxxx -o f    # 下载
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"file_ids":["f-xxxx"]}' http://<host>:8000/api/files/batch-delete
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"file_ids":["f-xxxx","f-yyyy"]}' http://<host>:8000/api/files/batch-download -o files.zip
```

- 文件库是**去重**的（同 md5+文件名复用）；删除不做引用校验，被删文件的任务执行时会因下载失败而失败。

## 6. 监控、取消与审计

### 6.1 任务状态

状态机：`queued → assigned → working → completed / failed / timed_out`，另有 `cancelled`。

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx/status
```

终态为 `completed`/`failed`/`timed_out`/`cancelled`；结束后读 `task.result`（`summary`/`exit_code`/`stdout_tail`/`stderr_tail`/`artifacts`/`session_id`）。轮询间隔建议 2–3 秒。

### 6.2 实时输出

边沿会把执行过程流式上报（LLM 的文本/工具调用，command 的原始输出行）：

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

- `queued`：直接移出队列，不会执行。
- `assigned`/`working`：edge 在约 3 秒内检测到并**杀掉整个进程组**（SIGTERM→SIGKILL），且不提交结果。
- 已终态：返回 `{"accepted": false}`（幂等）。

### 6.4 查找 / 清理 / 审计

```bash
# 列表（分页+筛选）
curl -s -H "Authorization: Bearer <token>" \
  "http://<host>:8000/api/tasks?status=working&mode=llm&search=hello&limit=50"
# 批量删除（按 id 或按条件）
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"all_matching":true,"status":"cancelled"}' http://<host>:8000/api/tasks/batch-delete
# 审计事件（dispatched / cancelled / permission_denied，含命中原因）
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/tasks/t-xxxxx/events
```

- `search` 匹配指令/任务 id/节点；`started_after`/`started_before` 为 ISO 时间。
- **审计事件**用于回答「这个任务为什么被拒/谁派的/何时取消」。

## 7. 技能库（给远程 LLM 用的专业知识）

技能是打包好的 `SKILL.md` 指南，供**边缘节点的 LLM 在任务中自主取用**。

- **何时用**：希望远程 llm 任务按某个专业流程/规范工作时。你通常**只需在 `instruction` 里提示**，边沿会自行浏览并下载技能。
- **何时不用**：任务简单、无需额外知识时。

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/skills          # 摘要
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/skills/<name>/download -o skill.zip
```

管理（Web「技能」页或 REST）：`POST /api/skills`（上传 zip，需含带 `name`/`description` frontmatter 的 `SKILL.md`；重传同名 version+1）、`PATCH /api/skills/{name}`、`DELETE /api/skills/{name}`。

**下载本 SKILL 的个性化版本**：`GET /api/skill-doc/agent-mesh` 会返回**已填好地址与你的 token** 的本文档，可直接复制其中的命令。

## 8. 模板与权限（一类节点的统一配置）

模板是可复用的节点配置，节点**引用式绑定**（`agents.template_id`）；改模板会同步到所有绑定节点。字段：`system_prompt`（提示词）、`llm_model`（默认模型）、`permission`（**权限**）、`data`（预留）。

- **何时用**：给「同一类用途」的多个节点统一模型/提示词/权限。单节点临时调整用 §9。

```bash
# 新建
curl -s -X POST http://<host>:8000/api/templates \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"name":"ops","description":"部署类节点","system_prompt":"你是部署专员。",
       "llm_model":"anthropic/deepseek-v4-flash"}'
# 列表 / 更新 / 删除
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/templates
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"system_prompt":"新提示词"}' http://<host>:8000/api/templates/2
curl -s -X DELETE -H "Authorization: Bearer <token>" http://<host>:8000/api/templates/2
# 绑定 / 解绑到节点
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"template_id":2}' http://<host>:8000/api/agents/3/template
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"template_id":null}' http://<host>:8000/api/agents/3/template
```

### 权限（llm 与 command 共用）

- 内置模板 `build`（全放开）/`plan`（禁改文件、只读命令、可联网）/`readonly`（仅读，无 shell/网络）；**默认全局 `default_permission` 为 `readonly`**。
- 未绑定模板的节点受该默认权限约束——**很多 command 会被拒**。要放开，给节点绑定 `build` 模板，或让 admin 改全局默认。
- 格式即 OpenCode `permission`：`{"edit":"deny","bash":{"*":"deny","ls *":"allow"}}`（规则**最后命中者生效**；白名单=`*:"deny"`+allow 列表，黑名单=`*:"allow"`+deny 列表）。command 模式下 `ask` 视为拒绝。
- 权限匹配器**只防误操作，不是安全边界**。

## 9. 节点级配置与操作

```bash
# 别名（null 清除）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"alias":"生产节点A"}' http://<host>:8000/api/agents/3/alias
# 描述（主 Agent 选节点依据）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"description":"生产 Web 服务器，只跑部署类命令"}' http://<host>:8000/api/agents/3/description
# 节点级 system prompt（与模板提示词拼接，节点在前）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"system_prompt":"你是运维专员，只操作 /opt。"}' http://<host>:8000/api/agents/3/system_prompt
# 节点级 LLM 配置（覆盖全局）
curl -s -X PATCH -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"llm_model":"anthropic/deepseek-v4-flash"}' http://<host>:8000/api/agents/3/llm_config
# 手动升级（空闲时自动下载新版并重启，失败自动回滚）
curl -s -X POST -H "Authorization: Bearer <token>" http://<host>:8000/api/agents/3/upgrade
```

**生效优先级**：模型 `节点 > 模板 > 全局`；提示词 `内置 + 节点 + 模板`；权限 `模板 > 全局默认（无节点级）`。配置改动经心跳（约 3 秒）下发。

## 10. 全局配置（admin）

```bash
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/settings
curl -s -X PATCH http://<host>:8000/api/settings \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"llm_model":"anthropic/deepseek-v4-flash","llm_base_url":"https://api.example.com/v1",
       "llm_api_key":"sk-...","llm_models":"anthropic/deepseek-v4-flash\nanthropic/deepseek-v4-pro",
       "default_permission":"{\"*\":\"allow\"}"}'
```

- `llm_models` 是**可用模型的唯一来源**（每行一个，llm 任务的 `model` 必须命中）。
- `public_url`：节点安装/回连用的对外地址。
- `max_concurrent`：每节点并发上限（默认 2）；`auto_upgrade`：自动升级开关；`default_permission`：未绑定模板节点的默认权限。

## 11. 安装 / 删除节点

安装（需**长期有效**的 token，不要用 24h session token）：

```bash
TOKEN='<长期 token>' bash <(curl -fsSL -H "Authorization: Bearer $TOKEN" \
  http://<host>:8000/api/bootstrap/install.sh)
```

- 目标机自动按 OS/ARCH 下载对应探针，注册开机自启并启动，以 device_id 注册为节点。
- 可选 `EDGE_ALIAS` 覆盖节点显示名。
- 删除节点会**向该机器下发卸载**并移除记录，谨慎：

```bash
curl -s -X DELETE -H "Authorization: Bearer <token>" http://<host>:8000/api/agents/3
```

## 12. 用户管理（admin）

```bash
curl -s -X POST http://<host>:8000/api/auth/users \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"username":"bob","password":"secret123","role":"user"}'   # 返回一次性 API token
curl -s -H "Authorization: Bearer <token>" http://<host>:8000/api/auth/users
curl -s -X POST -H "Authorization: Bearer <token>" http://<host>:8000/api/auth/users/bob/token  # 轮换
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"password":"newsecret456"}' http://<host>:8000/api/auth/users/bob/password
curl -s -X DELETE -H "Authorization: Bearer <token>" http://<host>:8000/api/auth/users/bob
curl -s -X POST -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"old_password":"...","new_password":"..."}' http://<host>:8000/api/auth/change-password
```

## 13. MCP 工具（SSE :8001）

MCP 通道**只接受用户 token**（session 或 API token）。工具与 REST 对应：

| 工具 | 用途 |
|------|------|
| `list_agents` / `get_agent` / `get_agent_detail` | 节点列表 / 单个 / 详情+最近任务 |
| `set_agent_alias` | 设置别名 |
| `list_models` | 可用 LLM 模型 id（llm 任务选 `model`） |
| `dispatch_task` | 派发任务 |
| `list_tasks` / `get_task_status` / `get_task_logs` | 任务列表 / 状态 / 实时输出 |
| `cancel_task` | 终止任务 |
| `list_skills` | 技能库摘要 |
| `poll_for_task` / `submit_result` | 边沿内部心跳，主 Agent 不用 |

> 文件库、模板管理、节点安装等**仅在 REST/Web**，MCP 无对应工具。

## 14. 完整流程（推荐节奏）

1. `GET /agents` → 选 `online=true` 且 `description` 匹配需求的节点，记下数字 `id`。
2. 若要处理本地文件：`POST /files` 上传拿 `file_id`（§5）。
3. `POST /tasks/dispatch` → 拿 `task_id`。
4. 需要进度：`GET /tasks/{task_id}/logs?after_id=<next_id>` 增量看输出。
5. 每 2–3 秒 `GET /tasks/{task_id}/status` 直到终态；用户要停就 `POST /tasks/{task_id}/cancel`。
6. 读 `task.result`；有 `artifacts` 就下载给用户（§5.4）。

## 15. 故障速查

| 现象 | 原因 / 处理 |
|------|-------------|
| `401 Unauthorized` | **token 过期/失效** → 向用户索取账号密码，重新 `POST /auth/login`（§1） |
| 派发 `403` / 任务摘要含「权限被拒绝」 | 节点权限策略拒绝该命令 → 说明情况，或改绑更宽松模板（§8） |
| 任务一直 `queued` | 目标节点离线或已达并发上限 → 换在线节点 / 等待 |
| `no LLM model configured` | 节点无可用模型 → 配置全局/节点/模板模型（§8/§10） |
| `attachment download failed` | 文件库文件被删或 md5 不符 → 重新上传（§5） |
| 找不到 `/api/skills`、模板等 MCP 工具 | 这些只在 REST，MCP 无对应工具（§13） |

## 16. 安全

- 保管 token；生产用 HTTPS；控制面用用户 token，不把全局 token 用于控制接口。
- LLM API key 仅存于服务端 DB 与节点 `edge.env`，经心跳下发；**不要写进代码/仓库/日志**。
- 删除节点会卸载远端 agent；取消任务会杀掉进程组——都是破坏性操作，执行前和用户确认。

## 参考

- 可运行示例：`references/example-poll.py`（登录→列节点→派发 command→轮询）。
