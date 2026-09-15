# 边沿 Agent 上报接口标准（edge-reporting）

> 本文档定义 **边沿 Agent → orchestrator** 的上报/自描述协议标准。它回答两个问题：
> 1. 探针上报了什么、怎么上报（字段规范）。
> 2. 收到新数据/新字段时，服务端如何追加与处理（治理流程）。
>
> 相关实现：`shared/schemas.py`（字段注册表）、`orchestrator/task_store.py:heartbeat`、`orchestrator/store/*`（注册表驱动持久化）、`edge/config_writer.py`（采集）。

---

## 1. 通道总览：上报只走一条通道

**节点自描述信息（系统信息 + 运行指标）只经 `POST /api/edge/poll_for_task` 心跳通道上报，永不新增上报端点。**

新增节点信息一律作为**字段**加进心跳 body，而不是新开 `/report_xxx` 接口。

| 通道 | 端点 | 职责 |
|------|------|------|
| **上报（自描述）** | `POST /api/edge/poll_for_task` | 心跳 + 身份/系统/指标上报 + 领任务（唯一上报通道） |
| 任务生命周期 | `POST /api/edge/mark_started` | 任务开始通知 |
| 任务生命周期 | `POST /api/edge/submit_result` | 任务结果上报 |
| 任务生命周期 | `POST /api/edge/get_task_status` | 边沿取消监视轮询 |
| 任务执行输出 | `POST /api/edge/task_log` | **LLM/命令实时执行输出流（v1.4.0）**：边沿解析 opencode JSONL 事件（text/error/complete/raw）批量上报，Web/REST 增量拉取 |
| 产物 | `POST /api/artifacts/{task_id}` | 产物上传（multipart） |

> 任务生命周期接口与产物上传和"节点自描述"职责不同，保持分开是对的——**不是接口膨胀**。`task_log` 属**任务执行输出流**（与 mark_started/submit_result 同类职责，随任务生命周期），不是"节点自描述信息"，因此**不受 §1「永不新增上报端点」约束**——该约束仅针对节点自描述字段（系统信息/运行指标），那些仍只走 `poll_for_task` 心跳通道。
> 已移除的历史冗余接口：`/api/edge/dispatch_task`、`/api/edge/cancel_task`（控制面已有等价接口，探针从未使用）。

---

## 2. 心跳 body 字段分组规范

`POST /api/edge/poll_for_task` body 按四组归类：

| 组 | 字段 | 说明 | 持久化策略 |
|----|------|------|-----------|
| `identity`（显式参数） | `agent_id`, `device_id` | 节点身份，`device_id` 为队列稳定键 | 必填，更新 |
| `identity`（显式参数） | `runtime`, `hostname`, `version` | 运行时/主机名/探针版本 | 更新 |
| `identity`（显式参数） | `running_tasks` | 当前正在执行的任务 id 列表（多任务并发重连恢复；v1.4.0，旧探针可缺省） | 不落库，仅用于并发分配 |
| `system`（静态） | `os`, `distro`, `arch` | 平台族/发行版/架构 | **缺失保留旧值**（COALESCE） |
| `metrics`（动态） | `cpu_percent`, `mem_percent`, `mem_used_mb`, `mem_total_mb` | 资源指标 | **有值则覆盖；字段缺失时保留旧值** |

- `os` 保持平台族（`linux`/`darwin`/`win32`），**用于升级包选型**（`agent-mesh-agent-{os}-{arch}.tar.gz`），不得改为发行版名。
- `distro` 为发行版信息（如 `ubuntu 22.04` / `centos 7` / `kylin V10`），Web 看板「系统」列显示 `distro || os`（老节点兜底）。
- `runtime`/`hostname`/`version` 与升级逻辑耦合，保持显式参数；`system`/`metrics` 两组由注册表驱动。

### 注册表（单一事实来源）

`shared/schemas.py` 定义上报字段清单与处理策略，**这是服务端唯一事实来源**：

```python
SYSTEM_FIELDS = ["os", "distro", "arch"]            # 静态：缺失保留旧值
METRIC_FIELDS = ["cpu_percent", "mem_percent", "mem_used_mb", "mem_total_mb"]  # 动态：有值覆盖、缺失保留旧值
TELEMETRY_FIELDS = SYSTEM_FIELDS + METRIC_FIELDS

def extract_telemetry(payload) -> dict   # 白名单提取，未知键忽略
def validate_telemetry(telemetry) -> None  # 非注册键抛 ValueError（防注入兜底）
```

- 注册表字段名 == `agents` 表列名（一对一对齐）。
- store 层（SQLite/PostgreSQL）的列名**只从注册表常量取值**，禁止拼接请求数据（防 SQL 注入）。
- `api/edge.py` 的 poll_for_task 经 `extract_telemetry()` 处理，管线内不出现具体字段名。
- **例外：服务端观测字段**。`agents.ip_address`（迁移 018）由服务端从 `request.client.host` 直接写入，**不来自探针上报、不进注册表**，因此不适用 §5 的 7 步流程；这类"服务端派生"字段以 `agents` 表列为准。

---

## 3. 服务端处理规则（兼容性铁律）

收到心跳 body 后，服务端按以下规则处理，**新旧版本探针/服务端可混跑**：

1. **未知字段 → 忽略**：`extract_telemetry` 白名单过滤，未知键不报错、不落库（前向兼容：新探针可上报服务端不认识的新字段）。
2. **缺失字段 → 保留旧值**：`system` 与 `metrics` 字段缺失或为 `None` 时均保留旧值（SQLite/PG 用 `COALESCE(?, col)`，缺失不覆盖；有值时覆盖）。
3. **坏数据 → 不拒绝心跳**：单字段类型异常不导致整次心跳失败（宽松写入，不做严格校验）。
4. **字段只增不改不删**：注册表字段是 append-only；改名/删除会破坏老探针上报。
5. **确需破坏性变更**：才引入 `proto_version` 字段（当前未启用），配合探针自升级收敛，届时在本文档声明版本迁移。

---

## 4. 响应侧指令通道

心跳响应固定携带以下指令字段（新增指令也走该通道，不新增端点）：

| 字段 | 说明 |
|------|------|
| `task` | 领取的任务（无则为 `null`；**兼容旧探针**，等于 `tasks[0]`） |
| `tasks` | 并发领取的任务数组（v1.4.0，多任务并发；旧探针只读 `task`） |
| `server_ts` | 服务端时间戳 |
| `max_concurrent` | 节点并发上限（默认 2，随心跳下发；v1.4.0） |
| `config` / `config_version` | 全局 + 节点级 LLM 配置同步（优先级：节点级 > 全局） |
| `agent_token` | agent 独立 token，仅首次注册返回一次 |
| `upgrade` | 自升级指令 `{version, filename}`（版本低于发布包时下发） |

---

## 5. 新增上报字段：标准 7 步流程

新增一个上报字段（如未来的 `kernel`、`cpu_model`）的唯一受支持方式：

1. **注册表登记**：在 `shared/schemas.py` 的 `SYSTEM_FIELDS`（静态，保留旧值）或 `METRIC_FIELDS`（动态，覆盖）中加一行。该行即声明了类型归属与持久化策略。
2. **数据库迁移**：新增 `orchestrator/store/migrations/0XX_<name>.sql`：`ALTER TABLE agents ADD COLUMN <field> <type>;`。
3. **PG schema 同步**：`orchestrator/store/connection/pg_schema_upgrade.py` 的 `ensure_agent_schema_upgrade()` 的 `additions` 字典加同名列。
4. **数据模型暴露**：`shared/schemas.py` 的 `AgentStatus` 加字段 + `model_dump_json_safe()` 输出。
5. **Web 展示**：`orchestrator/web_ui/js/agents.js` 列表/详情按需展示。
6. **测试**：`tests/test_edge_telemetry.py` 补一条心跳落库断言（含 keep-on-null / 覆盖策略）。
7. **文档**：更新本文档 §2 分组表 + `docs/` 相关设计文档。

> **不需要**改动：`task_store.py:heartbeat`、`store/*` 各后端的 upsert（已注册表驱动）、`api/edge.py`（已白名单提取）、探针采集（只在该探针版本新增采集逻辑）。

> **例外（v1.4.0）**：`running_tasks`（心跳 body 中边沿正在执行的任务 id 列表）是**控制面运行态**——不落库、不进注册表、不持久化到 `agents` 表，仅用于多任务并发分配（断线重连恢复 + 容量计算），因此**不适用**本 7 步流程（无需迁移/数据模型/看板改动）。它已在 §2 分组表登记为 `identity` 组的显式参数。

---

## 6. 探针侧采集

| 函数 | 位置 | 说明 |
|------|------|------|
| `get_os()` | `edge/config_writer.py` | 平台族 `linux`/`darwin`/`win32` |
| `get_distro()` | `edge/config_writer.py` | Linux 读 `/etc/os-release` → `ID VERSION_ID`（如 `ubuntu 22.04`）；非 Linux 返回 `None` |
| `get_arch()` | `edge/config_writer.py` | `x64` / `arm64` |
| `_collect_metrics()` | `edge/agent.py` | `psutil` 采集 CPU/内存指标，失败回退 `None` |

探针在每次心跳调用 `poll_for_task(...)` 时一并上报。新增系统字段后，老探针（未采集）不发该字段 → 服务端走"缺失保留旧值"规则，前后兼容。
