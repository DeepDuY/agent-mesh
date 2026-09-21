# 架构：目标、进程划分与目录结构

> 来源：原 DESIGN.md §1 / §2 / §16。

## 1. 进程划分与总体架构

### 1.1 进程清单

| 进程 | 数量 | 职责 | 入口 |
|------|------|------|------|
| 主 Agent | 外部 1+ | 通过 REST 派发与查询 | 无内置 |
| orchestrator | 每部署 1 | FastAPI REST+Web(:8000) + 后台清扫器（单进程） | `python -m agent_mesh.orchestrator.main` |
| 边沿 Agent | 每台远端机器 1 | 心跳拉任务、**多任务并发执行**（`max_concurrent` 默认 2）、产物上传、结果上报、实时输出上报 | 独立仓库 `agent-mesh-edge`（PyInstaller 二进制） |
| Web 浏览器 | 可选 | 看板轮询 | `http://<host>:8000/` |

### 1.2 架构图

```
                        ┌─ 主 Agent (DeepChat / OpenCode / Claude) ─┐
                        │              REST (:8000)                 │
                        └────────────────┬──────────────────────────┘
                                         ▼
        ┌────────────────────────────────────────────────────────────────┐
        │            orchestrator（单进程）                                │
        │                                                                │
        │  ├─ :8000 FastAPI（REST /api/* + /api/edge/* + Web 看板）       │
        │  └─ 后台清扫协程 (超时/离线扫描, 每 sweep_interval_s)             │
        │                                                                │
        │  存储后端 (AbstractStore 可插拔):                               │
        │  ├─ SQLiteStore (默认, WAL 模式) / store/sqlite/*              │
        │  └─ PostgresStore (可选, asyncpg) / store/pg/               │
        └────────────────────────────────────────────────────────────────┘
                           │                       │
            REST 心跳/领任务/提交                REST 查询 / Web 轮询
            (HTTPS / Bearer token)              (用户 token)
        ┌─────────────┼──────────────┐     ┌───────────────┐
        ▼             ▼              ▼     ▼               ▼
   ┌─────────┐  ┌─────────┐  ┌──────────┐  ┌────────────┐
   │ 边沿 A  │  │ 边沿 B  │  │ 边沿 N   │  │ Web 看板   │
   └────┬────┘  └────┬────┘  └────┬─────┘  │ (浏览器)    │
        │            │            │        └────────────┘
        ▼            ▼            ▼
     opencode     opencode     claude
      CLI          CLI          CLI
```

### 1.3 运行模式（单进程）

`orchestrator/main.py:main()` **只以单进程**运行：同一 asyncio 事件循环承载 uvicorn 与后台清扫器（`store.start_sweepers()`）。`config.workers` 默认 `1`，因此默认走 `_run_single_process()`。

> ⚠️ `AGENT_MESH_WORKERS>1` **会被忽略**：`_resolve_workers()` 检测到 `>1` 时打印警告并返回 `1`，服务仍以单进程启动。多 worker 需改用 import-string + `uvicorn.run`（或 supervisor）、把实时推送换成 PG `LISTEN/NOTIFY` 总线、并把 heartbeat 容量检查原子化，属架构改造，见 [known-issues.md §1](./known-issues.md)。

## 2. 目录结构

```
agent-mesh/
├── pyproject.toml
├── README.md
├── docs/                         # 设计文档（本文档体系）
│   ├── README.md
│   ├── architecture.md  protocol.md  task-and-execution.md
│   ├── orchestrator.md  auth-security.md  features.md
│   ├── deployment.md    known-issues.md
│   └── standards/edge-reporting.md     # 上报接口标准
├── data/                         # 运行数据（db/artifacts/bootstrap/skills/files + VERSION）
├── scripts/
│   ├── start-orchestrator.sh     # 后台启动 orchestrator
│   ├── migrate_sqlite_to_pg.py   # SQLite→PostgreSQL 迁移脚本
│   ├── sync_probe_release.py     # 从 GitHub Release 同步探针包
│   └── relocate_venv.py          # 重写 venv 符号链接（打包用）
├── src/agent_mesh/
│   ├── shared/{constants,schemas,permissions}.py
│   ├── orchestrator/
│   │   ├── main.py  task_store.py  tenancy.py  permissions.py  sweeper.py  scheduler.py  cron.py
│   │   ├── config.py  auth.py  artifact_store.py  realtime.py
│   │   ├── api/                   # REST 层（按领域拆分）
│   │   │   ├── __init__.py        # create_query_router + 认证依赖
│   │   │   ├── auth.py  tasks.py  agents.py  edge.py  artifacts.py  files.py  bootstrap.py  skills.py  schedules.py  realtime.py
│   │   │   ├── agent_common.py  agent_config.py  agent_access.py  agent_lifecycle.py
│   │   │   └── teams.py  templates.py
│   │   ├── store/
│   │   │   ├── base.py            # AbstractStore
│   │   │   ├── connection/{__init__,base,sqlite,pg,pg_schema,pg_schema_upgrade}.py
│   │   │   ├── sqlite/{__init__,connection,agents,tasks,artifacts,files,logs,settings,skills,users,teams,templates,schedules}.py
│   │   │   ├── pg/{__init__,base,agents,tasks,artifacts,files,logs,settings,skills,users,teams,templates,schedules}.py
│   │   │   └── migrations/001-022_*.sql
│   │   └── web_ui/{index.html,style.css,js/app,auth,agents,tasks,files,skills,schedules,users,teams,templates,config}.js
│   └── edge/                      # ← 已迁出：探针代码在独立仓库 agent-mesh-edge
├── deploy/
│   ├── install.sh                 # systemd 部署 orchestrator（edge 探针单独 bootstrap 安装）
│   ├── redeploy.sh  status.sh  uninstall.sh  common.sh
│   └── README.md
├── skills/agent-mesh/             # OpenCode skill（REST 用法索引 + references/）
│   ├── SKILL.md                   # 索引：场景速查 + 认证/环境 + 快速节奏
│   └── references/                # auth / nodes / tasks / files / skills / troubleshooting（.md）
└── tests/                         # pytest（含 test_task_store/test_auth_api/test_edge_telemetry/
                                   #  test_files_api/test_tenancy/test_permission/test_templates/
                                   #  test_edge_authz/test_upgrade 等）
```
