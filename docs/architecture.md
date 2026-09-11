# 架构：目标、进程划分与目录结构

> 来源：原 DESIGN.md §1 / §2 / §16。

## 1. 进程划分与总体架构

### 1.1 进程清单

| 进程 | 数量 | 职责 | 入口 |
|------|------|------|------|
| 主 Agent | 外部 1+ | 通过 MCP/REST 派发与查询 | 无内置 |
| orchestrator 主进程 | 每部署 1 | MCP SSE(:8001) + 后台清扫器 + uvicorn supervisor | `python -m agent_mesh.orchestrator.main` |
| orchestrator worker | `AGENT_MESH_WORKERS`(默认 cpu 核数) | FastAPI REST+Web(:8000)，多进程承载并发 | 同上（uvicorn 子进程） |
| 边沿 Agent | 每台远端机器 1 | 心跳拉任务、**多任务并发执行**（`max_concurrent` 默认 2）、产物上传、结果上报、实时输出上报 | `python -m agent_mesh.edge.agent`（或 PyInstaller 二进制） |
| Web 浏览器 | 可选 | 看板轮询 | `http://<host>:8000/` |

### 1.2 架构图

```
                        ┌─ 主 Agent (DeepChat / OpenCode / Claude) ─┐
                        │     挂载 MCP 客户端 (SSE 模式) 或 REST      │
                        └────────────────┬──────────────────────────┘
                                         │ MCP over SSE  (:8001)
                                         │ 或 REST       (:8000)
                                         ▼
        ┌────────────────────────────────────────────────────────────────┐
        │            orchestrator（多进程架构）                              │
        │                                                                │
        │  主进程:                                                        │
        │  ├─ :8001 MCP SSE (mcp_server.sse_app + Bearer 鉴权中间件, 13 个工具)                    │
        │  └─ 后台清扫协程 (超时/离线扫描, 每 sweep_interval_s)             │
        │                                                                │
        │  uvicorn workers (AGENT_MESH_WORKERS=N):                      │
        │  └─ :8000 FastAPI（REST /api/* + /api/edge/* + Web 看板）       │
        │                                                                │
        │  存储后端 (AbstractStore 可插拔):                               │
        │  ├─ SQLiteStore (默认, WAL 模式) / store/sqlite/*              │
        │  └─ PostgresStore (可选, asyncpg) / store/pg.py                │
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

### 1.3 多进程模式

`orchestrator/main.py:main()` 根据 `AGENT_MESH_WORKERS`（默认 `os.cpu_count()`）选择运行模式：

- **多进程（默认）**：`main()` 在主进程初始化后端 → `store.start_sweepers()` → 主进程内启动 MCP SSE；随后以 `uvicorn.Config(workers=N)` 启动 N 个 worker 子进程承载 FastAPI。sweeper 与 MCP SSE 只在主进程运行，避免多 worker 重复清扫。
- **单进程兼容（`workers=1`）**：同一 asyncio 事件循环并行运行 uvicorn 与 MCP SSE（原实现）。

多进程下 FastAPI lifespan 不再重复初始化后端或启动 sweeper（由主进程统一管理）。SQLite 通过 WAL 模式（`PRAGMA journal_mode=WAL`）+ `busy_timeout` 支持多进程并发读写。

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
├── mcp-config.example.json       # MCP over SSE 配置示例
├── data/                         # 运行数据（db/artifacts/bootstrap + VERSION）
├── scripts/
│   ├── start-orchestrator.sh     # 后台启动 orchestrator
│   ├── build-agent-bootstrap.py  # Agent 安装包构建（含 VERSION 清单）
│   ├── agent-mesh-edge.spec      # PyInstaller spec
│   ├── mcp_bridge.py             # MCP stdio ↔ SSE 桥（给 stdio 客户端）
│   ├── migrate_sqlite_to_pg.py   # SQLite→PostgreSQL 迁移脚本
│   └── relocate_venv.py          # 重写 venv 符号链接（打包用）
├── src/agent_mesh/
│   ├── shared/{constants,schemas}.py
│   ├── orchestrator/
│   │   ├── main.py  mcp_server.py  task_store.py
│   │   ├── config.py  auth.py  artifact_store.py
│   │   ├── api/                   # REST 层（按领域拆分）
│   │   │   ├── __init__.py        # create_query_router + 认证依赖
│   │   │   ├── auth.py  tasks.py  agents.py  edge.py  artifacts.py  files.py  bootstrap.py  skills.py
│   │   ├── store/
│   │   │   ├── connection.py  base.py  pg.py
│   │   │   ├── sqlite/{__init__,connection,agents,tasks,artifacts,files,logs,settings,skills,users}.py
│   │   │   └── migrations/001-012_*.sql
│   │   └── web_ui/{index.html,style.css,js/app,auth,agents,tasks,files,skills,users,config}.js
│   └── edge/
│       ├── agent.py  rest_client.py  config_writer.py
│       ├── execution/             # 任务执行（拆分）
│       │   ├── __init__.py        # Executor 门面
│       │   └── common.py  command.py  llm.py
├── deploy/
│   ├── install.sh                 # systemd 部署 orchestrator（edge 探针单独 bootstrap 安装）
│   ├── redeploy.sh  status.sh  uninstall.sh
├── skills/agent-mesh/             # OpenCode skill（REST 用法）
│   ├── SKILL.md
│   └── references/example-poll.py
└── tests/
    ├── test_task_store.py         # SQLite 状态机/清扫/取消测试
    ├── test_auth_api.py           # REST 认证/接口测试
    ├── test_edge_telemetry.py     # 上报字段注册表/持久化测试
    └── test_files_api.py          # 文件库/任务附件测试
```
