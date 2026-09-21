# agent-mesh

> 通用多 Agent 远程协同执行框架

**agent-mesh** 把你正在使用的 LLM Agent（DeepChat、OpenCode、Claude 等）与部署在任意机器上的执行节点连接起来：你用自然语言描述任务，agent-mesh 负责把任务排队、派发到目标节点执行，并回传结果、实时日志与产物。

## 为什么需要它

LLM Agent 很强，但它只能操作自己所在的那台机器。agent-mesh 让 Agent 拥有一个可管理的"远程机群"：

- 在目标机器上执行 shell 命令或自然语言任务，无需登录该机器
- 统一管理大量节点的在线状态、资源指标与版本
- 多用户 / 多团队隔离，任务、文件、节点按归属可见
- 全程可观测：实时日志、任务状态机、产物收集、审计事件

## 适用场景

- **跨机器运维**：批量巡检、部署、排障
- **分布式测试与构建**：把任务推到具备特定环境的节点
- **AI 驱动的自动化**：主 Agent 自主选择节点、连续执行多步任务
- **团队共享执行集群**：带租户与权限边界的统一算力入口

## 设计理念

- **通用、业务无关**：只提供调度与执行的骨架，具体做什么由 Agent 决定
- **边沿自包含**：节点探针打包运行时（含 opencode），目标机无需预装依赖或访问外网
- **安全默认**：分级 token、模板级执行权限、最小可见性
- **可观测**：任务状态机、实时输出、产物与审计全程留痕
- **部署简单**：服务端一条命令安装，节点一条命令接入，升级由调度器统一下发

## 架构

```
主 Agent (LLM)              orchestrator (FastAPI)              边沿节点
   │   REST /api/*  ──────▶  任务队列 · 节点心跳 · 持久化  ◀──────  │  心跳 /api/edge/*
   │   自然语言指令             SQLite / PostgreSQL                 │  opencode 执行
   └◀── 结果 / 日志 / 产物 ◀────────────────────────────────────────┘
```

- **主 Agent**：外部 LLM Agent，通过 REST 派发任务、查询状态
- **orchestrator**：单进程调度器，维护任务队列、节点心跳与持久化，内置中文 Web 看板
- **边沿 Agent**：部署在目标机器的轻量守护进程，拉取并执行任务、上传产物

## 核心特性

- **双执行模式**：`command`（shell 命令）与 `llm`（opencode 自然语言任务）
- **一键接入**：自包含探针支持 Linux / macOS，一条命令安装节点
- **自动升级**：调度器发布新版本后，节点空闲时自动升级，失败自动回滚
- **配置集中下发**：模型、密钥、提示词变更经心跳同步到所有在线节点
- **多任务并发**：单节点多任务隔离执行，支持 LLM 会话复用与实时输出
- **定时 / 周期任务**：按 cron 定期派发，带时区与错过/重叠策略
- **多租户与权限**：团队 / 用户隔离，模板级执行权限
- **技能库与文件库**：按需下发技能，任务附件自动校验
- **Web 看板**：节点、任务、文件、技能、用户、团队统一管理

## 快速部署

服务端（需要 root）：

```bash
git clone https://github.com/DeepDuY/agent-mesh.git
cd agent-mesh
sudo ./deploy/install.sh
```

安装完成后：

1. 打开 `http://<服务器IP>:8000/`，用默认账号 `admin / admin` 登录并**立即修改密码**
2. 在「配置」页填写 Public URL
3. 节点页点击「+ 安装新节点」，在目标机器执行生成的命令即可接入

> 部署脚本、常用参数与生产建议详见 [deploy/README.md](deploy/README.md)。

## 升级与卸载

```bash
./deploy/redeploy.sh     # 更新服务端并发布探针包
./deploy/uninstall.sh    # 卸载
```

节点升级由调度器统一管理：发布新版探针后，低版本节点会在空闲时自动升级。

## 文档

| 文档 | 内容 |
|------|------|
| [docs/README.md](docs/README.md) | 设计文档索引 |
| [docs/architecture.md](docs/architecture.md) | 架构与进程划分 |
| [docs/protocol.md](docs/protocol.md) | 通信协议与接口 |
| [docs/deployment.md](docs/deployment.md) | 部署与配置 |
| [docs/reference.md](docs/reference.md) | 环境变量、REST 接口与使用示例 |
| [skills/agent-mesh/SKILL.md](skills/agent-mesh/SKILL.md) | 主 Agent 操作指南 |

## 安全

- 生产环境务必修改默认密码，并创建专用账号
- 建议在 HTTPS 网关之后暴露服务，边沿节点使用低权限账号运行
- 详见 [docs/auth-security.md](docs/auth-security.md)

## License

MIT
