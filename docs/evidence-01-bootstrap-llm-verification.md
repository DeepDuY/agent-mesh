# 核验记录 01：`GET /api/bootstrap/install.sh` 不内嵌任何 LLM 配置

> 对应评审澄清点「bootstrap 脚本是否包含 LLM 配置」。本文件记录**当前默认分支**（版本 v1.4.2）下
> 对该端点**实际生成的脚本**的核验过程与结果，作为仓库内证据。
> 核验时间：2026-09-02（UTC）；版本：源码 `shared/constants.py` VERSION = `data/bootstrap/VERSION` = 在线节点上报版本 = **1.4.2**。
>
> ⚠️ 本文为**历史核验快照**（v1.4.2）。当前版本以 `shared/constants.py:VERSION` 为准（现为 1.6.2）；文中节点/版本结论勿当作当前现状引用。

## 结论

**不包含。** 对当前运行实例实测：生成的 `install.sh` 全文 22 行，`grep -ciE "EDGE_LLM|llm_api_key|sk-[a-z0-9]|api[_-]key"` 命中数为 **0**。
脚本内只出现 `INSTALL_URL` / `ORCHESTRATOR_URL` / `TOKEN`（环境变量）/ `EDGE_ALIAS`（可选），**无任何 LLM 凭据或 LLM 配置字段**。

LLM 配置（`llm_api_key` / `llm_base_url` / `llm_model`）在新节点注册后经**心跳 config-sync** 下发
（见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)），不经过安装脚本。

## 核验步骤与实测

```bash
# 1) 登录拿短期 session token（此处脱敏）
TOKEN=<session-token>

# 2) 拉取当前默认分支实际生成的安装脚本
curl -s -H "Authorization: Bearer $TOKEN" \
  http://<orchestrator>:8000/api/bootstrap/install.sh -o install.sh

# 3) 关键词扫描：期望 0 命中
grep -ciE "EDGE_LLM|llm_api_key|sk-[a-z0-9]|api[_-]key" install.sh   # => 0

# 4) 完整性
md5sum install.sh   # 核验时点实测：5783aca42b3cce6b766c1703a9461958
```

## 实测生成的脚本全文（v1.4.2，2026-09-02）

```bash
#!/usr/bin/env bash
set -e
INSTALL_URL="http://127.0.0.1:8000/api/bootstrap/agent-mesh-agent-linux-x64.tar.gz"

OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
    x86_64) ARCH="x64" ;;
    aarch64|arm64) ARCH="arm64" ;;
esac

ORCHESTRATOR_URL="http://127.0.0.1:8000"
if [ -z "${TOKEN:-}" ]; then
    echo "ERROR: TOKEN environment variable is required" >&2
    exit 1
fi

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
curl -fsSL -H "Authorization: Bearer $TOKEN" "$INSTALL_URL" -o "$TMPDIR/agent.tar.gz"
tar -xzf "$TMPDIR/agent.tar.gz" -C "$TMPDIR"
INSTALL_DIR="${INSTALL_DIR:-/opt/agent-mesh-agent}" ORCHESTRATOR_URL="$ORCHESTRATOR_URL" TOKEN="$TOKEN" EDGE_ALIAS="${EDGE_ALIAS:-}" bash "$TMPDIR/agent-mesh-agent-linux-x64/install.sh"
```

## 代码依据

- 模板唯一真源：`src/agent_mesh/orchestrator/api/bootstrap.py::bootstrap_install_script`
  - 仅拼接：`INSTALL_URL`（base_url + 包名）、`ORCHESTRATOR_URL`（base_url）、`TOKEN`、`EDGE_ALIAS`；
  - 源码注释明确：LLM 配置**有意不内嵌**，注册后经心跳 config-sync 下发；
  - 安装脚本要求调用方提供 `TOKEN`（`if [ -z "${TOKEN:-}" ]`），无 token 直接报错退出。
- 包内安装脚本 `data/bootstrap/install.sh` 亦无硬编码 key：`EDGE_LLM_API_KEY=${EDGE_LLM_API_KEY:-}`（从环境变量读，默认空）。
- 自动化测试：`tests/test_config_sync_upgrade.py::test_bootstrap_install_script_does_not_embed_llm_key`
  —— 配置全局 `llm_api_key` 后请求本端点，断言响应体不含 `sk-` / `llm_api_key` / `EDGE_LLM`。
- LLM 配置安全下发链路：见 [auth-security.md §9](./auth-security.md#9-llm-配置同步) 与 [deployment.md §1](./deployment.md)。

## 文档一致性（结论唯一）

当前分支全部相关文档均表述为"**不再内嵌 LLM 配置**"，无相互矛盾：
`docs/protocol.md`、`docs/orchestrator.md`、`docs/deployment.md`、`docs/auth-security.md`、
`docs/known-issues.md`（§7 已修复项）、`README.md`（"安装后自动写入 edge.env 的 LLM 由心跳同步"）。

> 说明：评审所见的"文档内嵌全局 LLM 配置"冲突描述来自 **v1.3.3 快照提交包**中的历史文档
> （该版本文档修正尚未合入）。1.4.x 已统一为上述唯一结论；本仓库当前默认分支即本核验对象。
