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

### B. 修改探针（edge/ 下的代码）

探针代码（`src/agent_mesh/edge/**`）是**打包进安装包、通过心跳自升级分发**的，只改源码不会自动生效。修改后**必须**：

1. **升版本**：递增 `src/agent_mesh/shared/constants.py` 的 `VERSION`（升级判定基准，见 [auth-security.md §8](./auth-security.md#8-agent-自升级)）；
2. **重建安装包**：`uv run --no-sync python scripts/build-agent-bootstrap.py`，刷新 `data/bootstrap/VERSION` 与 `agent-mesh-agent-{os}-{arch}.tar.gz`（需 venv 已装 pyinstaller；`--no-sync` 避免 uv 重新同步触发 asyncpg 源码编译，见 [deployment.md §1.1](./deployment.md)）；
3. 在线节点在**空闲时自动升级**（或看板「升级」手动触发）；探针升级后按规则 A 同步接口相关文档；
4. 若改动涉及探针上报字段，另按 [standards/edge-reporting.md §5](./standards/edge-reporting.md#5-新增上报字段标准-7-步流程) 走注册表 + 迁移流程。

> 忘记重打包 = 探针永远跑旧代码；忘记升版本 = 已发布包与 `data/bootstrap/VERSION` 不一致，升级判断失效。

### C. 修改 Web 看板（`web_ui/`）

按 [standards/ui-copy.md](./standards/ui-copy.md) 检查文案：**前端只写用户操作/校验/安全提示，不写功能设计或内部机制**。字段含义用标题旁 `?` tooltip，不铺成段落。UI 文案变更无需升探针版本。
