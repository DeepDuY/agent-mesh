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

> 从 GitHub 拉下源码后直接 `./deploy/install.sh` 即可。构建探针需要 `opencode`：默认从本机
> (`~/.opencode/bin/opencode` / `PATH`) 自动探测，**没有则自动从 opencode 官方 GitHub releases 下载**
> （`github.com/sst/opencode/releases/latest`）。如需指定本机二进制或跳过打包：
> ```bash
> ./deploy/install.sh --opencode /path/to/opencode   # 指定 opencode
> ./deploy/install.sh --no-probe                     # 只装服务端，不打包探针
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
│   └── bootstrap/                # 探针安装包 + VERSION（节点安装/升级用）
└── log/
```

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

```bash
python scripts/build-agent-bootstrap.py \
    --output-dir /opt/agent-mesh/data/bootstrap \
    --opencode /path/to/opencode
```

- `opencode` 默认必带；确实不需要时可加 `--allow-missing-opencode`（此时 llm 任务在节点上不可用）。
- 目标平台按当前机器自动检测，可用 `--os` / `--arch` 覆盖。

## 卸载

```bash
./deploy/uninstall.sh
```

## 防火墙 / 网络

- REST/Web 端口：`8000`

确保外部主 Agent 能访问该端口（MCP SSE :8001 已随通道移除）。
