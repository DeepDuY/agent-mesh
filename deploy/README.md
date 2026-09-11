# 部署指南

## 一键安装

```bash
cd <repo>
./deploy/install.sh
```

安装后自动：
- 部署到 `/opt/agent-mesh`
- 创建 systemd 服务并设置开机自启
- 生成随机 token（保存在 `/opt/agent-mesh/etc/orchestrator.env`）

## 目录结构

```
/opt/agent-mesh/
├── bin/                      # 启动脚本
│   ├── agent-mesh-orchestrator
│   └── agent-mesh-edge
├── lib/agent-mesh/           # 源码
├── lib/venv/                 # Python 虚拟环境
├── etc/                      # 配置文件
│   ├── orchestrator.env
│   └── edge.env
├── log/                      # 日志
│   ├── orchestrator.log
│   └── edge.log
├── run/                      # pid 文件（systemd 管理）
└── data/                     # 运行时数据
    └── artifacts/
```

## 服务管理

```bash
# 启动
systemctl start agent-mesh-orchestrator
systemctl start agent-mesh-edge

# 停止
systemctl stop agent-mesh-orchestrator
systemctl stop agent-mesh-edge

# 查看状态
./deploy/status.sh

# 查看日志
tail -f /opt/agent-mesh/log/orchestrator.log
tail -f /opt/agent-mesh/log/edge.log
```

## 升级/重新部署

```bash
./deploy/redeploy.sh
```

## 卸载

```bash
./deploy/uninstall.sh
```

## 多 Edge Agent

编辑 `/opt/agent-mesh/etc/edge.env` 中的 `EDGE_AGENT_ID`，然后创建额外的 systemd 服务实例。或者复制服务文件并修改 `EnvironmentFile` 路径。

## 防火墙/网络

- REST/Web 端口：`8000`
- MCP SSE 端口：`8001`

确保外部主 Agent 能访问这两个端口。
