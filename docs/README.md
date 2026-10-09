# agent-mesh 文档

> 多 Agent 远程协同框架的完整设计文档（由原 `DESIGN.md` 拆分而来）。框架通用、业务无关。

## 文档导航

| 文档 | 内容 |
|------|------|
| [architecture.md](./architecture.md) | 项目目标与组成、进程划分与总体架构、目录结构 |
| [protocol.md](./protocol.md) | 进程间交互总览、节点标识、通信协议（REST / 上报） |
| [task-and-execution.md](./task-and-execution.md) | 任务模型与双执行模式、任务状态机、边沿 Agent 内部逻辑 |
| [orchestrator.md](./orchestrator.md) | orchestrator 内部逻辑、数据模型、持久化层与迁移 |
| [auth-security.md](./auth-security.md) | 认证与安全、agent 独立 token、自升级、LLM 配置同步 |
| [features.md](./features.md) | LLM 会话复用、技能库、资源指标 |
| [deployment.md](./deployment.md) | 一键安装（bootstrap）、配置项、启动与部署 |
| [reference.md](./reference.md) | 环境变量、REST 接口清单与使用示例 |
| [known-issues.md](./known-issues.md) | 已知问题与后续演进 |
| [standards/edge-reporting.md](./standards/edge-reporting.md) | **标准**：边沿 Agent 上报接口规范与字段治理（新增字段流程） |
| [standards/ui-copy.md](./standards/ui-copy.md) | **标准**：Web 看板文案规范（禁止在前端展示功能设计/内部机制） |

## 概览

构建一个通用的多 Agent 远程协同执行框架：

- **主 Agent（外部进程）**：你自己的 LLM Agent（DeepChat、OpenCode、Claude 等）。用自然语言"帮我在那台机器跑个测试"，主 Agent 通过 REST（:8000）调用 orchestrator 完成派发与查询。
- **orchestrator（独立调度器/Broker，单进程）**：持有任务队列、维护边沿节点心跳状态、持久化到 SQLite/PostgreSQL。当前以单进程运行（FastAPI :8000 与后台清扫器同一事件循环）；`AGENT_MESH_WORKERS>1` 会被忽略并按单进程运行（多 worker 待设计，见 [known-issues.md](./known-issues.md)）。
  - **FastAPI（:8000）**：REST 查询/管理接口 `/api/*` + 边沿 REST 协议 `/api/edge/*` + Web 看板 `/` + `/static`。
- **边沿 Agent（每台远端机器一个守护进程）**：循环 HTTP 心跳拉取任务，调用本机已安装的 **opencode**（或 `claude`）执行任务，上传产物、回报结果。
- **Web 浏览器（可选）**：静态页面，每 5 秒轮询 REST 接口展示节点/任务/配置。

各模块的设计细节见上述文档。

## 变更规范

任何修改都必须按下面的规则同步产物与文档，否则会让主 Agent / 边沿探针拿到过期版本而行为不一致。

### A. 修改 REST 接口（新增/变更端点或参数）

必须同步更新：

1. `skills/agent-mesh/SKILL.md` 及其 `references/*.md` —— 主 Agent 使用指南（索引、场景速查、端点表、派发参数、示例）；
2. 本文档体系：`protocol.md`（接口清单）→ `features.md`（功能说明）→ `orchestrator.md`（数据模型/迁移）→ `reference.md`（环境变量与接口参考）；
3. 涉及边沿上报字段时按 [standards/edge-reporting.md §5](./standards/edge-reporting.md#5-新增上报字段标准-7-步流程) 走注册表流程；
4. 涉及探针行为（如附件下载/校验）时同步更新 `task-and-execution.md`。

> 缺失同步会让主 Agent 拿到过期的 SKILL.md 而用错接口。

### B. 修改探针（edge）

探针代码**不在本仓库**：它位于独立仓库 **`agent-mesh-edge`**（本仓库只含 orchestrator 服务端，已不含 `src/agent_mesh/edge/**`）。修改探针必须：

1. **在 edge 仓库修改并升版本**：递增 `agent-mesh-edge` 仓库根 `VERSION`（升级判定基准，见 [auth-security.md §8](./auth-security.md#8-agent-自升级)）；
2. **在该仓库重建安装包**：`scripts/build-agent-bootstrap.py`（本地或用其 CI：push 触发 `build-probe`，产出 `probe-v<VERSION>` Release）；
3. **同步到服务端**：`POST /api/bootstrap/sync`（或 `python scripts/sync_probe_release.py`）把成品包拉到 `data/bootstrap/`；在线节点在**空闲时自动升级**（或看板「升级」手动触发）；
4. 若改动涉及探针上报字段，另按 [standards/edge-reporting.md §5](./standards/edge-reporting.md#5-新增上报字段标准-7-步流程) 走注册表 + 迁移流程。

> 本地开发目录与构建命令见根目录 `AGENTS.md`（机器相关的本地说明，未纳入版本控制）。
> **忘记重打包 = 探针永远跑旧代码；忘记升版本 = 已发布包与 `data/bootstrap/VERSION` 不一致，升级判断失效。**

### C. 修改 Web 看板（`web_ui/`）

按 [standards/ui-copy.md](./standards/ui-copy.md) 检查文案：**前端只写用户操作/校验/安全提示，不写功能设计或内部机制**。字段含义用标题旁 `?` tooltip，不铺成段落。UI 文案变更无需升探针版本。

### D. 修改数据模型 / 新增列（关键：DDL 执行顺序）

新增/变更表结构时，**必须同时覆盖「全新建库」与「旧库增量升级」两条路径**，并严格遵守执行顺序：

- **`create_schema`（`connection/pg_schema.py`）在 `ensure_*_schema_upgrade`（`connection/pg_schema_upgrade.py`）之前执行**（见 `connection/pg.py::initialize`）。
- `create_schema` 里用的是 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`——对**已存在的旧表是 no-op**。因此**任何引用新列的东西（索引、唯一约束、外键、CHECK、触发器）都不许写进 `create_schema`**：旧库上该列还不存在，会直接抛 `asyncpg.exceptions.UndefinedColumnError`，**服务启动失败**（2026-09 的 `idx_tasks_run_id` 就是这样把生产打挂的）。
- 规则：**先加列，再建依赖它的约束/索引**，且都在对应的 `ensure_*_schema_upgrade` 里；`create_schema` 只负责"全新库的最终形态"，不负责旧库增量。

新增一个列的检查清单（缺一不可）：

1. `create_schema` 的 `CREATE TABLE` 里加上该列（**供全新库**；不要在同一个 `execute` 块里建依赖它的索引）。
2. `ensure_<域>_schema_upgrade` 的 `additions` 字典里加上 `ALTER TABLE ... ADD COLUMN`（**供旧库**）。
3. 该列上的索引/约束写在 upgrade 函数里，且**位于加列之后**（同一函数内顺序靠后即可；`create_schema` 里不写）。
4. SQLite 侧新增 `store/migrations/NNN_*.sql`（按序号升序执行），同样遵守"先加列再建索引"。
5. 对照 `docs/orchestrator.md §3.1 迁移文件清单` 与 `§3.3 表清单` 同步文档。
6. **验证不能只用全新 SQLite**（全新库有列，掩盖问题）：至少人工核对 `create_schema` 与 upgrade 的执行顺序；条件允许时对一份"旧 schema"的 PG 库跑一次启动。

> 一句话：**新列的索引永远放 upgrade 函数里（加列之后），`create_schema` 里只放表定义。**

### E. 时间字段（统一 UTC + 前端换算）

- **存储与接口一律 UTC**：PG 会话时区为 `Etc/UTC`，列用 `TIMESTAMP`（无时区）；写入前经 `_pg_param` 把 aware datetime 转成 naive UTC。
- **接口输出必须带时区偏移**：对外序列化的 datetime 要经过 `_iso_to_dt`（naive 视为 UTC）或用 `_utcnow()`，使其 `isoformat()` 输出 `+00:00`。**不要直接把 asyncpg 返回的 naive datetime 透传**（用户的 `users`/`files` 等 dict 返回路径曾漏掉，导致前端把 UTC 当本地时间显示）。
- **前端显示**：统一用 `app.js` 的 `parseServerTime()` / `formatDateTime()` / `toLocaleDateTime()`——缺时区偏移时按 UTC 处理，再换算到浏览器本地时区；**禁止**用 `.replace('T',' ').slice(0,19)` 之类原样截断（会显示 UTC 而非本地时间）。

