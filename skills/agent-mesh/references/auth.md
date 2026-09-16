# 认证、地址与安全

## 地址

- **REST Base URL**：`http://<orchestrator-host>:8000/api`
- 不知道地址就问用户；Web 控制台「配置」页的「公开地址」就是它。本技能包的 `.env` 里 `AGENT_MESH_BASE_URL` 即此地址。

## 凭据：用 `.env` 里的用户 token

控制接口（REST）需要**用户 token**，通过请求头携带：

```bash
set -a; . ./.env; set +a
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/agents"
```

两种 token：

| 类型 | 来源 | 有效期 |
|------|------|--------|
| **API token** | 用户在 Web 控制台「个人中心」生成/轮换 | 生成时可选（默认**永久**） |
| **session token** | `POST /auth/login`（账号+密码） | 默认 24 小时 |

> 本技能包分发的是**用户 API token**，放进 `.env`，无需账号密码即可长期使用（除非用户设置了有效期）。

登录（仅当没有可用 token 时）：

```bash
curl -s -X POST http://<orchestrator-host>:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<用户名>","password":"<密码>"}'
# => {"username":"...","token":"<token>","role":"...","token_type":"session"}
```

## ⚠️ 收到 401 怎么办（重要）

**任何请求返回 `401 Unauthorized`，就是 token 过期或失效。**

1. **不要**反复重试、不要猜测密码、不要把密码写进日志/仓库。
2. **明确向用户说明**：「编排器 token 已过期/失效，请在控制台『个人中心』重新生成 token，并把新 token 更新到 `.env`」。
3. 用户给出新 token 后，替换 `.env` 中的 `AGENT_MESH_TOKEN` 再继续。
4. 若用户希望免于轮换，可在「个人中心」生成**永久** token。

## 安全

- **可见性按身份隔离**：你只能看到/操作被授权的节点，以及自己（或本团队）的任务与文件；无权访问的资源返回 404（如同不存在）。若需要访问某节点/数据，请让管理员授权，**不要尝试绕过**。
- 保管好 token；生产环境用 HTTPS。
- **不要调用管理面接口**：模板/权限、全局配置、用户管理、节点编辑（改别名/描述、升级、安装/删除节点）。它们对普通 API 调用不开放或属于管理员职责。
- 破坏性操作（取消任务等）执行前和用户确认。
