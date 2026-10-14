# 认证与请求约定

技能包根目录的 `.env` 包含 `AGENT_MESH_BASE_URL`（末尾为 `/api`）和用户 `AGENT_MESH_TOKEN`。所有示例在该目录执行：

```bash
set -a; . ./.env; set +a
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents"
```

未配置地址时向用户获取编排器公开地址。API token 在 Web 控制台「个人中心」生成，可能设置到期时间；登录所得 session token 默认有效期为 24 小时。用户需登录时调用：

```bash
curl -sS -X POST "$AGENT_MESH_BASE_URL/auth:login" \
  -H "Content-Type: application/json" \
  -d '{"username":"<用户名>","password":"<密码>"}'
```

## 访问与错误

| 情况 | 处理 |
|---|---|
| `401` | 凭据无效或过期；请用户在「个人中心」更新 API token 和 `.env`，停止以旧凭据重试。 |
| `403` | 请求被节点权限、命令策略或审核策略拒绝；查看响应 `detail`。 |
| `404` | 资源不存在或当前身份无访问权限；核对 ID，必要时请求管理员授权。 |

普通用户 token 只允许访问已授权节点、本人或团队可见的任务，以及本人创建的文件；管理员可查看全部。不要把 `.env`、token 或密码写入日志与仓库；连接优先使用 HTTPS。管理面配置由管理员通过 Web 平台完成。
