# 部署指南

## 一键安装（服务端 + 探针包）

```bash
cd <repo>
./deploy/install.sh
```

脚本会：

1. 部署 orchestrator 到 `/opt/agent-mesh`（systemd 服务、开机自启）。
2. 生成随机全局 token 写入 `/opt/agent-mesh/etc/orchestrator.env`（**已存在则保留，不会覆盖**）。
3. 设置数据路径到 `/opt/agent-mesh/data`（DB / artifacts / bootstrap）。
4. **构建并发布 edge 探针安装包**（含 `opencode`）到 `/opt/agent-mesh/data/bootstrap/`，供节点安装与自动升级。

> 探针源码在**独立仓库 `agent-mesh-edge`**，本仓库不含。构建探针需要该仓库的 checkout：默认自动探测同级 `../agent-mesh-edge`，也可 `--edge-repo DIR` / `EDGE_REPO_DIR=...` 指定；其 `.venv` 里需装 `pyinstaller` 等构建依赖。opencode 默认从本机探测，没有则自动从官方 GitHub releases 下载。不需要在服务端构建时可加 `--no-probe`，之后用看板/`POST /api/bootstrap/sync` 同步成品包：
> ```bash
> ./deploy/install.sh --edge-repo /path/to/agent-mesh-edge   # 指定 edge 仓
> ./deploy/install.sh --opencode /path/to/opencode           # 指定 opencode
> ./deploy/install.sh --no-probe                             # 只装服务端，不打包探针
> ```
> 探针包在服务端目录：`/opt/agent-mesh/data/bootstrap/agent-mesh-agent-<os>-<arch>.tar.gz`。

## 目录结构

```
/opt/agent-mesh/
├── bin/agent-mesh-orchestrator   # 启动脚本
├── lib/agent-mesh/               # 源码
├── lib/venv/                     # Python 虚拟环境
├── etc/orchestrator.env          # 配置 + 全局 token
├── data/
│   ├── agent-mesh.db             # SQLite（默认）
│   ├── artifacts/                # 任务产物
│   ├── files/  skills/           # 文件库 / 技能库落盘
│   └── bootstrap/                # 探针安装包 + VERSION（节点安装/升级用）
└── log/                          # 预留；服务日志实际走 systemd journal
```

> 服务日志走 systemd journal（`journalctl -u agent-mesh-orchestrator`）；`./deploy/status.sh` 直接从 journal 读取最近日志（无 unit/日志不可用时回退到 `log/*.log` 文件）。

## 服务管理

```bash
./deploy/status.sh                                  # 状态 + 最近日志 + 端口
systemctl {start,stop,restart,status} agent-mesh-orchestrator
```

## 升级 / 重新部署

```bash
./deploy/redeploy.sh                 # 更新服务端 + 发布 repo/data/bootstrap 里已有的探针包
BUILD_PROBE=1 ./deploy/redeploy.sh   # 同时重新构建探针包（需 opencode + PyInstaller）
```

- `redeploy.sh` 不改 `orchestrator.env`（token/配置保留）。
- 若 repo 的 `data/bootstrap/` 里有探针包，会同步发布到服务端；否则提示如何构建。

## 单独构建探针包

在 **`agent-mesh-edge` 仓库**中构建（本仓库不含构建脚本）：

```bash
cd agent-mesh-edge
.venv/bin/python scripts/build-agent-bootstrap.py \
    --output-dir /opt/agent-mesh/data/bootstrap \
    --opencode /path/to/opencode
```

- `opencode` 默认必带；确实不需要时可加 `--allow-missing-opencode`（此时 llm 任务在节点上不可用）。
- 本机没有 opencode 时默认从 GitHub 自动下载；不希望联网下载可加 `--no-download-opencode`。
- 目标平台按当前机器自动检测，可用 `--os` / `--arch` 覆盖。

## 卸载

```bash
./deploy/uninstall.sh
```

## 防火墙 / 网络

- REST/Web 端口：`8000`

确保外部主 Agent 能访问该端口（MCP SSE :8001 已随通道移除）。
