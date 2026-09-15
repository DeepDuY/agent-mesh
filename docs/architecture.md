# 架构：目标、进程划分与目录结构

> 来源：原 DESIGN.md §1 / §2 / §16。

## 1. 进程划分与总体架构

### 1.1 进程清单

| 进程 | 数量 | 职责 | 入口 |
|------|------|------|------|
| 主 Agent | 外部 1+ | 通过 REST 派发与查询 | 无内置 |
| orchestrator | 每部署 1 | FastAPI REST+Web(:8000) + 后台清扫器（单进程） | `python -m agent_mesh.orchestrator.main` |
| 边沿 Agent | 每台远端机器 1 | 心跳拉任务、**多任务并发执行**（`max_concurrent` 默认 2）、产物上传、结果上报、实时输出上报 | `python -m agent_mesh.edge.agent`（或 PyInstaller 二进制） |
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

`orchestrator/main.py:main()` 当前**实际以单进程**运行：同一 asyncio 事件循环承载 uvicorn 与后台清扫器（`store.start_sweepers()`）。注意 `config.workers` 默认值为 `os.cpu_count()`，因此默认会走 `_run_multi_process()` 分支，但 uvicorn 仍只起 1 个进程（见下）。

> ⚠️ `AGENT_MESH_WORKERS>1` 目前**不生效**：`main.py` 使用 `uvicorn.Config(workers=N)` 配合 `uvicorn.Server(config).serve()`，而该版本 uvicorn 的 `Server.serve()` 忽略 `workers`，实际只启动 1 个 server 进程。多 worker 需改用 import-string + `uvicorn.run`（或 supervisor），属架构改造，见 [known-issues.md §1](./known-issues.md)。

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
│   ├── build-agent-bootstrap.py  # Agent 安装包构建（含 VERSION 清单）
│   ├── agent-mesh-edge.spec      # PyInstaller spec
│   ├── migrate_sqlite_to_pg.py   # SQLite→PostgreSQL 迁移脚本
│   └── relocate_venv.py          # 重写 venv 符号链接（打包用）
├── src/agent_mesh/
│   ├── shared/{constants,schemas,permissions}.py
│   ├── orchestrator/
│   │   ├── main.py  task_store.py  tenancy.py  permissions.py  sweeper.py
│   │   ├── config.py  auth.py  artifact_store.py
│   │   ├── api/                   # REST 层（按领域拆分）
│   │   │   ├── __init__.py        # create_query_router + 认证依赖
│   │   │   ├── auth.py  tasks.py  agents.py  edge.py  artifacts.py  files.py  bootstrap.py  skills.py
│   │   │   └── teams.py  templates.py
│   │   ├── store/
│   │   │   ├── base.py            # AbstractStore
│   │   │   ├── connection/{__init__,base,sqlite,pg,pg_schema,pg_schema_upgrade}.py
│   │   │   ├── sqlite/{__init__,connection,agents,tasks,artifacts,files,logs,settings,skills,users,teams,templates}.py
│   │   │   ├── pg/{__init__,base,agents,tasks,artifacts,files,logs,settings,skills,users,teams,templates}.py
│   │   │   └── migrations/001-018_*.sql
│   │   └── web_ui/{index.html,style.css,js/app,auth,agents,tasks,files,skills,users,teams,templates,config}.js
│   └── edge/
│       ├── agent.py  rest_client.py  config_writer.py
│       ├── execution/             # 任务执行（拆分）
│       │   ├── __init__.py        # Executor 门面
│       │   └── common.py  command.py  llm.py
├── deploy/
│   ├── install.sh                 # systemd 部署 orchestrator（edge 探针单独 bootstrap 安装）
│   ├── redeploy.sh  status.sh  uninstall.sh  common.sh
│   └── README.md
├── skills/agent-mesh/             # OpenCode skill（REST 用法）
│   ├── SKILL.md
│   └── references/example-poll.py
└── tests/                         # pytest（含 test_task_store/test_auth_api/test_edge_telemetry/
                                   #  test_files_api/test_tenancy/test_permission/test_templates/
                                   #  test_edge_authz/test_upgrade 等）
```
