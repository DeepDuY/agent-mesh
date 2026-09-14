# orchestrator 内部逻辑、数据模型与持久化

> 来源：原 DESIGN.md §8 / §10 / §11。

## 1. orchestrator 内部逻辑

### 1.1 main.py：装配与运行

- `create_app(config=None, store=None, artifact_store=None) -> (app, store, artifact_store)`：
  - 默认用 `SQLiteStore(config.db_path)` 包成 `TaskStore`（`sweep_interval_s` 默认 5s、`offline_after_s` 默认 15s）；测试可注入临时 SQLite 背书的 TaskStore。
  - `artifact_store` 默认 `ArtifactStore(artifact_dir, max_size_mb, max_total_mb)`。
  - FastAPI：`title="agent-mesh-orchestrator"`、`version="1.0.0"`、`redirect_slashes=False`。
  - 挂载：`query_router`（`create_query_router(config, store, artifact_store)`，前缀 `/api`）、`/static`、`/` → `index.html`。
  - `lifespan`：启动时初始化后端 + `store.start_sweepers()`；退出时 `stop_sweepers()` + 关闭后端。
- `main()`：创建 app 与 `create_mcp_server(config, store)`，在**同一事件循环**用 `asyncio.gather` 同时跑：
  - `uvicorn`（:8000，REST/Web）
  - `_run_sse_server(mcp_server, config, host, port+1, store)`（:8001，MCP SSE，`sse_path="/"`、`message_path="/messages/"`）。
  - ⚠️ `_run_sse_server` 自行构建 Starlette app（`mcp_server.sse_app(...)`）并套一层 **Bearer 鉴权中间件**（`resolve_token_user`，见 [auth-security.md §1](./auth-security.md#1-现状)），SSE 与 message 两个端点都要求用户 token。

### 1.2 mcp_server.py：MCP 工具层

- `MCPServer(name="agent-mesh-orchestrator", version=VERSION)`，13 个工具（见 [protocol.md §3.1](./protocol.md#31-主-agent--orchestratormcp-工具sse-8001)）。版本取自 `shared.constants.VERSION`。
- 所有工具直接读写共享的 `TaskStore`；参数校验失败返回 `{"accepted": false, "error": ...}`。
- 认证**不在 MCP 工具层做**，由传输层中间件强制校验用户 token。
- `poll_for_task` 的上报字段经 `extract_telemetry()` 白名单处理（见 [standards/edge-reporting.md](./standards/edge-reporting.md)）。

### 1.3 task_store.py：状态机 + 心跳注册 + 清扫

- `dispatch()`：
  1. 校验 `mode ∈ {command, llm}`；
  2. 校验 `depends_on` 中的 task_id 都存在（仅校验，不持久化、不阻塞，见 [known-issues.md §3](./known-issues.md)）；
  3. `_resolve_agent(agent_ref)` 解析目标 → `queue_key = device_id or agent_id`；
  4. 创建 `Task(status=queued, agent_id=queue_key)`，`create_task` + `enqueue(task_id, queue_key)`。
- `_resolve_agent(agent_ref)`：`int(agent_ref)` → `get_agent_by_id`；否则 `get_agent(agent_ref)`（按 device_id）；再回退 `list_agents` 里 `agent_id == agent_ref` 的最新一条。
- `heartbeat()`：见 [protocol.md §1.5](./protocol.md#15-心跳领取与断线重连)。上报字段（system/metrics）以 `telemetry: dict` 传入，键经注册表白名单校验，store 层据此持久化（见 [standards/edge-reporting.md](./standards/edge-reporting.md)）。
- `mark_started()`：仅 `assigned` → `working`，写 `started_at`。
- `submit_result()`：仅 `assigned/working` 接受；写 `task_results`、状态置 `result.status`、`finished_at`、清 `current_task`。
- `cancel_task()`：仅非终态任务可取消——`queued` 先 `remove_from_queue()`；`assigned/working` 直接置 `cancelled`。取消后边沿 agent 的下一次 cancel 轮询会终止正在执行的子进程。
- **并发模型**：TaskStore 本身不加锁（原 `asyncio.Lock` 已删除）；并发正确性依赖后端存储的原子操作——SQLite 每次操作持自身 `asyncio.Lock`，`dequeue` 在锁内完成"取队首+删行"。`update_task_status(..., expected_status=...)` 支持原子条件更新（追加 `WHERE status = <expected>`），用于需要"仅在当前状态为 X 时才迁移"的场景（如超时清扫，见 §1.4）。

### 1.4 后台清扫协程（每 `sweep_interval_s` 一轮）

**`_sweep_timeouts()`**（超时检测，列表取自 `status=working`，limit 1000）：
```
for 候选 in working列表:
    task = get_task(候选)               # 重新取，避免用陈旧快照
    if task 不存在 或 status != working: continue   # 防止覆盖刚完成的任务（TOCTOU）
    if started_at + timeout_s > now: continue
    # 原子条件更新：仅当 status 仍为 working 才置 timed_out
    marked = update_task_status(task, timed_out, finished_at=now, expected_status=working)
    if not marked: continue             # 边沿已提交结果/任务已终态 → 不覆盖真实结果
    置结果 failed(-1, "task timed out")  # marked 成功后才写 task_results、清 current_task
    if retry_count < max_retries:
        status=queued; assigned/started/finished=None; retry_count+1; enqueue   # 重试
```
> 原子性：`update_task_status` 支持可选 `expected_status` 参数，追加 `WHERE status = <expected>`，保证"标记超时"与"边沿提交结果"之间的竞态不会互相覆盖（2026-09-01 修复，见 [known-issues.md §18](./known-issues.md)）。

**`_sweep_offline()`**（离线检测，遍历所有 agents；v1.4.0 起为多任务并发版）：
```
if last_seen 为空:
    if online: set_agent_online(False)
elif now - last_seen > offline_after_s:
    active = list_active_tasks(agent)            # 该 agent 全部 assigned/working 任务
    if 存在任一 status==working: continue          # 执行中，保持在线，不回队任何任务
    for 每个 active 中 status==assigned 的任务:      # 离线回队所有已分配未开始的任务
        置 queued; assigned_at=None; enqueue; 若等于 current_task 则清 current_task
    if online: set_agent_online(False)
```
> 多任务说明：不再只处理单个 `current_task_id`，而是把该 agent 的**全部 ASSIGNED 任务**回队；只要有任一 WORKING 任务即整体跳过（实现见 `task_store.py::_sweep_offline`）。

### 1.5 api/ 包：REST 层

REST 端点按领域拆分到 `orchestrator/api/` 包，`__init__.py` 的 `create_query_router()` 统一挂载：

- `api/__init__.py`：`create_query_router()` + 认证依赖（`require_user_token` / `require_any_token`）+ `_store_artifact_ref`。
- `api/auth.py`：`POST /api/auth/login`、`GET /api/auth/me`、`GET /api/healthz`、admin 用户管理（创建/列表/删除/轮换 token/重置密码）+ 自助改密。
- `api/tasks.py`：任务列表（含 `offset` 分页与 `total` 计数）/详情/状态/派发/终止/单删/批量删除（`batch-delete` 支持 `task_ids` 与 `all_matching` 两种模式）。
- `api/agents.py`：节点列表/详情/别名/LLM 配置/独立 token 轮换/升级请求/删除（`_get_agent_by_numeric_or_string_id`）。
- `api/edge.py`：边沿协议（`poll_for_task`/`submit_result`/`mark_started`/`get_task_status`）。
- `api/artifacts.py`：产物上传/列表/下载。
- `api/files.py`：文件库上传/列表/下载/删除（见 [features.md §5](./features.md#5-文件库任务附件)）。
- `api/bootstrap.py`：`/settings`、`/bootstrap/install.sh`、`/bootstrap/{filename}`。

关键行为：
- 认证：`require_user_token`（用户 token：API token 按 SHA-256 查表，session token 按 HMAC 解析，禁用用户拒绝）；`require_admin`（admin 角色限定）；`require_any_token`（全局 `config.token` / 用户 token / **agent 独立 token** 任一，返回调用者身份 `{auth:...}`）。Edge 端点、产物上传、bootstrap 下载用后者。详见 [auth-security.md](./auth-security.md)。
- 登录：`POST /api/auth/login` 校验 bcrypt，返回**短期 session token**（不再返回长期 API token）。
- 产物上传：`artifact_store.save()` 写磁盘（`threading.Lock` 内串行做大小/总量校验与写盘，避免并发超配额）+ `await` 同步落库元数据。
- 删除节点（`DELETE /api/agents/{id}`）：先下发 command 自毁命令（见 [task-and-execution.md §4.2](./task-and-execution.md#42-apitaskspy--apiagentspy)），随后立即删 DB 记录。
- `GET /api/bootstrap/install.sh`：动态生成安装脚本（见 [deployment.md §1](./deployment.md)），内嵌 orchestrator `base_url`；**不再内嵌全局 LLM 配置**（v1.3.3 起改由心跳 config-sync 下发，见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)）。
- `GET /api/bootstrap/{filename}`：静态文件服务，目录为 `<db_path 父目录>/bootstrap`（鉴权为 `require_any_token`，edge 可用全局 token 下载升级包）。

### 1.6 存储层（orchestrator/store/）

- `base.py`：`AbstractStore` 抽象接口 + `_new_task_id()`（`t-<uuid8>`）。接口含 `list_tasks(..., offset=0)` 与 `remove_from_queue()`。
- **`connection.py`（统一数据库对接层，v1.4.0）**：集中管理两侧连接生命周期与执行原语，store 层只写 SQL + 行映射：
  - `Database` 抽象：`execute`（返回 dict-like 行）/ `execute_rowcount`（写返回受影响行数）/ `fetchrow`（单行）/ `executemany` / `dequeue`。**所有 SQL 统一用 `?` 占位符**。
  - `SQLiteDatabase`：每连接新建 + `asyncio.Lock` 串行化；WAL（`PRAGMA journal_mode=WAL` + `busy_timeout=5000`）；SQL 迁移（`store/migrations/*.sql`）；`_ensure_admin_user`/`_ensure_session_secret`。
  - `PostgresDatabase`：**per-process 惰性 asyncpg 池**（`_acquire()` 按 `os.getpid()` 建池，fork 的 uvicorn worker 自动重建自己的池，规避 asyncpg loop 绑定与 fork 陷阱）；`initialize()` 用一次性连接建 schema + 列级升级；`?`→`$n` 占位符转换；aware datetime 自动归一化为 naive（匹配 PG `TIMESTAMP` 无时区列）。
  - 统一工具：`_dt_to_iso`/`_iso_to_dt`/`_load_json`/`_dump_json`/`_utcnow`。
- `sqlite/`：`SQLiteStore` 生产实现，按数据域拆分为多个 mixin（`agents.py`/`tasks.py`/`artifacts.py`/`settings.py`/`users.py`/`skills.py`/`files.py`/`logs.py`）。mixin 基类 `SQLiteBase` 持有 `self._db: SQLiteDatabase`，`_execute`/`_execute_rowcount` 委托统一层；所有写方法基于 `rowcount` 返回正确布尔值；`dequeue` 走统一层原子"取队首+删行"。
  - **上报字段持久化是注册表驱动的**：`upsert_agent(..., telemetry=...)` 的列名只从 `shared.schemas` 的 `SYSTEM_FIELDS`/`METRIC_FIELDS` 常量取；`system` 字段用 `COALESCE(?, col)` 保留旧值、`metrics` 字段直接覆盖（见 [standards/edge-reporting.md](./standards/edge-reporting.md)）。
- `pg.py`：`PostgresStore` 可选生产实现，持有 `self._db: PostgresDatabase`，只做行映射与 SQL。`dequeue` 走统一层 `FOR UPDATE SKIP LOCKED` 原子出队；幂等建表（`IF NOT EXISTS`）+ 列级 schema 升级。设置 `AGENT_MESH_DB_TYPE=pg` + `AGENT_MESH_PG_DSN` 启用。**多进程支持**：每个 worker 进程在统一层内自动重建连接池。
- `auth.py`：`hash_password` / `verify_password`（bcrypt）/ `generate_token`（`secrets.token_urlsafe(32)`）。

## 2. 数据模型（shared/schemas.py）

### 2.1 上报字段注册表

`SYSTEM_FIELDS` / `METRIC_FIELDS` / `TELEMETRY_FIELDS` 常量 + `extract_telemetry()` / `validate_telemetry()`。见 [standards/edge-reporting.md §2](./standards/edge-reporting.md#2-心跳-body-字段分组规范)。

### 2.2 Task
```python
class Task(BaseModel):
    task_id: str                      # t-<uuid8>
    agent_id: str                     # 目标稳定键 = device_id 或 agent_id（resolve 后写入）
    mode: Literal["command", "llm"] = "llm"
    instruction: str
    constraints: Constraints
    status: TaskStatus                # queued/assigned/working/completed/failed/timed_out
    created_at: datetime
    assigned_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    retry_count: int = 0
    result: TaskResult | None = None
    max_retries: int = 0
    attachments: list[FileRef] = []   # 派发时引用的文件库快照（tasks.attachments JSON 列）
    # model_dump_json_safe(): 输出 JSON 安全字典（含 constraints、result、attachments）
```

### 2.3 Constraints
```python
class Constraints(BaseModel):
    workdir: str = "."
    timeout_s: int = 300
    model: str | None = None
    output_limit: int = 200_000
    session_id: str | None = None   # 选填：续用 opencode 会话（llm 模式）
    skills: list[str] | None = None # 预留：任务级技能提示（当前 skills 由提示词指引自主取用）
```

### 2.4 TaskResult
```python
class TaskResult(BaseModel):
    status: Literal["completed", "failed"]
    mode: Literal["command", "llm"] = "llm"
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    artifacts: list[ArtifactRef] = []
    duration_ms: int
    summary: str
    session_id: str | None = None   # 本次实际使用的 opencode 会话 ID（llm 模式）
```

### 2.5 ArtifactRef
```python
class ArtifactRef(BaseModel):
    filename: str
    size: int
    content_type: str
    download_url: str      # /api/artifacts/{task_id}/{artifact_id}
    artifact_id: str       # a-<uuid8>
```

### 2.5.1 FileRef（文件库 / 任务附件）
```python
class FileRef(BaseModel):
    file_id: str           # f-<uuid8>
    filename: str          # 原文件名（净化后 basename）
    size: int
    content_type: str
    md5: str               # 上传时计算，探针下载后校验
    download_url: str      # /api/files/{file_id}
```

### 2.6 AgentStatus / AgentDetail
```python
class AgentStatus(BaseModel):
    id: int                            # 数字自增主键
    agent_id: str                      # 显示名，不唯一
    device_id: str | None              # machine-id，设备唯一键
    alias: str | None
    runtime: str | None
    hostname: str | None
    os: str | None
    distro: str | None                 # 发行版（如 ubuntu 22.04），上报注册表驱动
    arch: str | None                   # x64 / arm64（升级包选型）
    version: str | None                # 上报的 agent 版本（升级判定）
    online: bool
    last_seen: datetime | None
    current_task_id: str | None
    llm_api_key / llm_base_url / llm_model: str | None   # 节点级 LLM 覆盖（读回，见 auth-security.md §9）
    upgrade_requested: bool            # 升级请求标记（见 auth-security.md §8）
    upgrade_version: str | None        # 目标版本
    cpu_percent / mem_percent / mem_used_mb / mem_total_mb: float | None   # 心跳资源指标
    # model_dump_json_safe(): 额外输出 display_name = alias or hostname or agent_id

class AgentDetail(AgentStatus):
    tasks: list[dict]                  # 最近 20 个任务
    metadata: dict | None              # detail 里携带 {"allowed_users": [...]}（见 auth-security.md §7）
    created_at / updated_at: datetime | None
```

> `token_hash` 不在 `AgentStatus` 中暴露（敏感字段，仅存 DB）。

### 2.7 TaskStatus（shared/constants.py）
```python
class TaskStatus(str, enum.Enum):
    QUEUED="queued"; ASSIGNED="assigned"; WORKING="working"
    COMPLETED="completed"; FAILED="failed"; TIMED_OUT="timed_out"
    CANCELLED="cancelled"          # 人为终止（终态）
```

## 3. 持久化层与迁移

### 3.1 迁移文件

```
store/
├── base.py              # AbstractStore 接口 + _new_task_id()
├── connection.py        # 统一数据库对接层：Database 抽象 + SQLiteDatabase + PostgresDatabase（v1.4.0）
├── sqlite/              # SQLite 生产实现（按数据域拆分为 mixin）
│   ├── __init__.py      # SQLiteStore 组合类
│   ├── connection.py    # SQLiteBase 薄壳（持有 SQLiteDatabase，委托 _execute/_execute_rowcount）
│   ├── agents.py        # AgentMixin
│   ├── tasks.py         # TaskMixin + QueueMixin
│   ├── artifacts.py     # ArtifactMixin
│   ├── files.py         # FileMixin
│   ├── logs.py          # TaskLogMixin
│   ├── settings.py      # SettingsMixin
│   ├── skills.py        # SkillsMixin
│   └── users.py         # UsersMixin
├── pg.py                # PostgresStore（可选，持有 PostgresDatabase）
└── migrations/
    ├── 001_initial.sql              # 全量初始表 + 默认 admin（占位 hash）
    ├── 002_add_task_mode.sql        # tasks/task_results 增加 mode
    ├── 003_agent_id_numeric.sql     # agents 重建为数字 id + mac 唯一键
    ├── 004_settings.sql             # settings 表 + 默认值
    ├── 005_device_id.sql            # mac → device_id（UNIQUE 索引）
    ├── 006_users_token_hash.sql     # users 重建：删除明文 token，改存 token_hash
    ├── 007_agent_upgrade_config_sync.sql  # agents 增加 arch/version/升级标记 + config_version
    ├── 008_agent_token_users.sql   # agents.token_hash + agent_users
    ├── 009_skills_session_metrics.sql  # tasks.session_id/skills、task_results.session_id、agents 资源列、skills 表
    ├── 010_agent_distro.sql        # agents.distro（发行版上报字段）
    ├── 011_files_attachments.sql   # files 表（文件库）+ tasks.attachments（附件快照列）
    └── 012_task_logs.sql           # task_logs 表（LLM 实时执行输出流）
```

### 3.2 迁移机制
- SQLite `_run_migrations()` 按文件名前缀序号升序执行，`schema_migrations(version)` 记录已应用版本，`executescript` 原子执行，幂等。
- PG `_ensure_agent_schema_upgrade()` / `_ensure_user_schema_upgrade()` 用 `information_schema.columns` 做列级增量升级（`ALTER TABLE ADD COLUMN`），新列只需在 `additions` 字典登记。

### 3.3 表清单
| 表 | 说明 | 备注 |
|----|------|------|
| `schema_migrations` | 迁移版本 | |
| `users` | 账号（`username` 唯一、`token_hash` 唯一，**不含明文 token**） | 含 `disabled/created_by/last_login_at/token_created_at`；默认 admin |
| `agents` | 节点注册表（`id` 自增、`device_id` UNIQUE） | 含 `llm_api_key/base_url/model` 列（节点级 LLM 覆盖，随心跳 config-sync 下发，见 auth-security.md §9）及 `distro` |
| `tasks` | 任务（**无外键**，`agent_id` 存稳定键） | |
| `task_queue` | 队列（`task_id` UNIQUE，外键级联删除） | |
| `task_results` | 结果（`task_id` UNIQUE） | |
| `artifacts` | 产物元数据 | `storage_path` 落盘路径 |
| `files` | 文件库（file_id 主键、md5 去重） | 落盘 `<db_path>/../files/`；`tasks.attachments` 存引用快照 |
| `task_logs` | 实时执行输出流（`id` 自增、`task_id`、`kind`、`content`） | `kind`: text/error/complete/raw；随任务删除级联 |
| `task_events` | 事件审计表 | **已无代码写入**（`log_event` 已移除，预留） |
| `settings` | 全局配置（`public_url`/`llm_*`） | |
