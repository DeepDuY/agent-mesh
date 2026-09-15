# 认证与安全

> 来源：原 DESIGN.md §12。

## 1. 现状

| 面 | 现状 |
|----|------|
| REST 查询/Web | 用户 token（`HTTPBearer`）：API token（SHA-256 查表）**或** session token（HMAC 签名解析） |
| `/api/edge/*`、产物上传、bootstrap 下载 | 全局 `config.token` **或** 用户 token **或** agent 独立 token（§7） |
| 边沿 Agent | 独立 agent token（§7）；旧节点可用全局 token / 用户 token 兼容 |

## 2. 密钥与哈希

- 密码：bcrypt（`passlib`）。
- API token：`generate_token()`（`secrets.token_urlsafe(32)`）创建，**仅以 SHA-256 摘要（`hash_token()`）入库**；创建/轮换时明文只返回一次。
- session token：登录时签发，`make_session_token(username, session_secret, ttl_s)` = `base64url(payload).HMAC-SHA256` 无状态签名，含 `exp` 过期时间；`session_secret` 首次启动随机生成并持久化到 `settings` 表。**多 worker 安全**（无服务端会话存储）。**fail-closed**：若 `session_secret` 缺失/为空，登录直接拒绝（HTTP 500），`verify_session_token()` 对空 secret 一律返回 `None`——绝不使用空 secret 签发或校验 token。
- token 对比：`hmac.compare_digest` 常数时间比较。

## 3. 默认管理员

- 迁移写入占位 hash，`_ensure_admin_user()` 启动时检查：仅当 hash 为占位符/非法时重置为 `hash_password("admin")`——**已改密码不会在重启时被复位**。
- admin 的 API token 同样在启动时引导：若 `token_hash` 缺失或仍为占位符摘要，则生成随机 token 并在日志中打印一次（`generated a new admin API token (shown once)`）。
- admin 可通过 `POST /api/auth/change-password` 修改密码、`POST /api/auth/users/{username}/token` 轮换 token。
- Web 看板登录框**不再预填** `admin/admin`，需手动输入（避免弱口令一键登录）。

## 4. 用户管理（admin）

- `POST /api/auth/users` 创建用户：校验用户名（`[A-Za-z0-9_.-]`）、角色（`admin`/`user`）**与 `team_id`（必填，且团队必须存在——一个用户只属于一个团队）**，返回一次性 API token；`GET /api/auth/users` 列表（不含任何 token/hash，含 `team_id`/`team_name`）；`DELETE /api/auth/users/{username}` 删除（禁止删 admin 与自身；同时清理 `team_members`/`agent_users` 幽灵关联）；`POST /api/auth/users/{username}/token` 轮换；`POST /api/auth/users/{username}/password` 重置密码；`POST /api/auth/change-password` 自助改密。
- 禁用用户（`users.disabled=1`）后：REST 鉴权拒绝（401），登录拒绝（403）。

## 5. LLM apiKey

- 存入 `settings` 表（全局）或写入 `workdir/opencode.json`（任务级临时文件，执行后删除）。
- ✅ 安装脚本**不再内嵌** LLM 配置：`GET /api/bootstrap/install.sh` 只生成不含任何凭据的引导脚本，新节点注册后由心跳 config-sync 下发 LLM 配置（配置同步见 §9）。

## 5.1 安装脚本用途差异与 API Key 落盘

**三个安装相关脚本用途（edge 探针不经 `deploy/install.sh`）：**

| 脚本 | 用途 | LLM key |
|------|------|---------|
| `deploy/install.sh` | 仅安装 **orchestrator** 到 `/opt/agent-mesh`（systemd）；升级用 `redeploy.sh` | 无任何 LLM key 字段；不读不写数据库中的 `llm_api_key` |
| `GET /api/bootstrap/install.sh`（外层引导） | 远端机器一键装 edge 探针 → 下载安装包 → 调包内 `install.sh` | 不含 LLM 配置（v1.3.3 修复后） |
| `data/bootstrap/install.sh`（包内） | 把 edge 装到 `/opt/agent-mesh-agent` | `EDGE_LLM_API_KEY` 从环境变量读、默认空，无硬编码 |

**API Key 落盘路径（运行态，不入 git/脚本）：**
- LLM API Key：orchestrator `settings` 表（DB）、任务级临时 `workdir/opencode.json`（用后即删）、边沿节点 `etc/edge.env`（config-sync 下发后持久化）。
- 全局 token：orchestrator `etc/orchestrator.env`、边沿 `etc/edge.env`。
- 用户/agent token：DB 存 SHA-256 哈希；agent token 明文仅签发时返回一次。

**风险控制**：`deploy/install.sh` 不含 edge 与任何 LLM key 字段；引导脚本不内嵌 LLM 配置（含自动化测试断言）；密钥哈希存储；`data/`、`etc/`、`*.log`、`.env` 均被 `.gitignore` 排除。

## 6. 执行隔离

- 模板级 `permission`（OpenCode `permission` 规格）+ edge 共享匹配器（llm 与 command 共用）；内置 `build`/`plan`/`readonly`，默认 `readonly`。**匹配器只防误操作，不是安全边界**；生产建议低权限账号/容器。
- 拒绝与关键动作写入 `task_events` 审计（`GET /api/tasks/{id}/events`）。
- `delete_agent` 下发自毁命令属于高风险操作，Web 端需二次确认。

## 7. Agent 独立 token 与设备-用户关联

**设计目标**：agent 用**自己的独立 token** 认证，与用户登录/session 完全解耦——用户的 session token 过期不会导致边沿失联（此前 401 事故根因）。

- 存储：`agents.token_hash`（SHA-256，仅存摘要），迁移 008。token 明文只在签发/轮换时返回一次。
- 签发：`ensure_agent_token(agent_id)` 在 agent **首次成功注册**（第一次心跳）时生成 `secrets.token_urlsafe(32)` 并入库，明文在 poll 响应 `agent_token` 字段返回**一次**；边沿收到后 `persist_edge_token()` 写入 `edge.env`（`EDGE_TOKEN=`）并 `client.set_token()` 切换，之后全部用它认证。
- 轮换：`POST /api/agents/{id}/token`（用户 token）→ 新 token 返回一次，旧 token 立即失效。
- 认证：`require_any_token` 依次接受 全局 token → 用户 token → agent token（`get_agent_by_token_hash`），并返回身份 `{auth: "global"|"user"|"agent", ...}`。**agent token 绑定 device_id**：当请求携带的 `device_id` 与绑定值不一致时拒绝（403）；若请求未带 `device_id`，则回退按 `agent_id` 定位，不触发该绑定校验。
- 兼容：旧二进制（无 token 持久化逻辑）仍可用全局/用户 token 认证；本轮 401 由"手动把 agent token 写入 edge.env"解决，升级到新二进制后自动持久化。
- **设备-用户关联（多用户管理底座）**：`agent_users(agent_id, user_id)` 多对多表（迁移 008）。agent 注册时若调用方是用户 token（`{auth:"user"}`）则自动把该用户关联为该机器的操作者；`GET /api/agents/{id}/detail` 的 `metadata.allowed_users` 返回关联用户列表（**仅 admin**；非 admin 调用返回空列表）。**多用户权限管控已实现**：节点 ACL `agents.access = {"teams":[...], "users":[...]}`（迁移 017）+ `TaskStore.can_access_agent()` 在派发/查看时按用户或团队放行（admin 全权）；模板/团队管理见多租户章节。

## 8. Agent 自升级

- 触发：`POST /api/agents/{id}/upgrade`（或看板「升级」按钮）写入 `upgrade_requested/upgrade_version`（迁移 007），目标版本 = `data/bootstrap/VERSION`。**自动升级**：心跳时若节点上报版本低于当前 bootstrap 版本且 `auto_upgrade` 设置开启（默认 `1`，配置页可关），无需手动请求即下发升级指令（`api/edge.py`）。
- 下发：心跳响应在 `upgrade_requested` 且包存在时携带 `upgrade: {version, filename}`（filename 由 agent 的 `os/arch` 拼出）；节点上报版本 ≥ 目标版本时自动 `clear_agent_upgrade`。
- 执行（edge **完全空闲**时，即无任何正在执行的任务 `_running` 为空，v1.4.0 多任务并发下同样只在空闲才升级）：从 `/api/bootstrap/{filename}` 下载（`bootstrap_download` 已改为 `require_any_token`，全局 token 可下）→ staging 校验解包 → 备份 `.bin.old` 与 `agent_version.bak` → **用 `os.replace()` 原子替换** `bin/agent-mesh-edge.bin`/`opencode`（⚠️ 不能原地 `copy2` 覆盖——运行中的进程会报 Linux `Text file busy`）→ 写回滚感知 wrapper（`WRAPPER_SCRIPT`）→ 写 `etc/upgrading` 标记与 `etc/agent_version` → **优先 `systemctl restart agent-mesh-edge`（Linux）/ `launchctl kickstart system/com.agentmesh.edge`（macOS）重启**，失败回退 `os.execv(wrapper)`。⚠️ 只替换磁盘文件、进程仍在旧代码（execv 在 PyInstaller onefile 下不可靠）是 1.3.0 之前升级"假成功"的根因，服务管理器重启才是可靠路径。
- 回滚（两阶段握手）：① 首次重启时 wrapper 发现 `upgrading` 存在且无 `upgrade-started` → 记录 `upgrade-started` 并运行新二进制；② 新二进制**首次成功心跳**后由 edge 清除标记与备份（确认健康）；③ 若新二进制启动失败、systemd 再次拉起 wrapper 时 `upgrading` + `upgrade-started` 同时存在 → 恢复 `.bin.old` 与旧版本号。
- 前提：旧二进制无 `_perform_upgrade`，**无法自升级**，只能重装新包；新二进制装好后即可看板点升级。

## 9. LLM 配置同步

- 触发：`PATCH /api/settings` 保存 `llm_*` / `llm_models`，或 `PATCH /api/agents/{id}/llm_config`、`PATCH /api/agents/{id}/system_prompt`、`PATCH /api/agents/{id}/template`、`/api/templates` 的增删改（节点级/模板级覆盖）→ `config_version` 自增（`TaskStore.bump_config_version`）。
- 下发：心跳响应携带 `config_version` 与解析后的 `config`（`_resolve_edge_config`）；含 `llm_api_key/base_url/model`、`llm_models`、`system_prompt`、`permission`。
- 应用（edge）：对比本地 `etc/config_version`，变更时更新运行中的 `Executor`，并 `apply_llm_config()` 重写 `edge.env` 的 `EDGE_LLM_*`、把 `system_prompt`/`llm_models` 写入 `etc/system_prompt`、`etc/llm_models`，把 `permission` 写入 `etc/permission.json`（多行安全，不破坏 shell source）+ 落盘版本号 → 重启后仍生效。
- **模型名与规模可配置（2026-09-11 起）**：`settings.llm_models` 是唯一模型来源（换行/逗号分隔的完整网关 id），配置页可编辑；edge `build_opencode_config()` **不再硬编码任何模型**，完全按该列表生成 opencode `models` map（键=去 `anthropic/` 前缀、`name`=末段）。per-task 下发仍要求 `llm_model`/`Constraints.model` 为网关真实完整 id（如 `anthropic/deepseek-v4-flash`），否则 404/5xx（§15 教训）。
- **强制配置（无默认兜底）**：已删除所有硬编码默认模型（`EdgeConfig.llm_model`、`_DEFAULT_LLM_MODEL`、migration 004 / PG `_ensure_settings` 的种子均改为空串）。未配置默认模型且未显式传 `model` 时，`mode=llm` 的派发在 orchestrator 侧即被拒绝（REST 400），edge `run_llm` 亦会 fail-fast。`Constraints.model` 显式指定且 `llm_models` 非空时必须命中列表。
- **节点模板与提示词层级（2026-09-11 起）**：新增 `templates` 表 + `agents.template_id`（引用式绑定，删除模板自动解绑）。解析优先级：模型 `节点 llm_model > 模板 llm_model > 全局 settings.llm_model`；提示词 `节点 system_prompt + 模板 system_prompt`（拼接，节点在前；内置 wrapper 由 edge 负责）。**已移除全局 `settings.system_prompt`**（其行在迁移 014 / PG 启动时删除）。
- **提示词注入（edge）**：`_wrap_llm_instruction(instruction, system_prompt)` 把拼接后的节点+模板提示词作为「## 角色与上下文」块注入每个 llm 任务提示词；为空则不注入。
- **重装防坑**：install.sh 重装时会清理 `etc/config_version` 等状态文件，否则新二进制本地版本==服务端版本会跳过同步、沿用旧模型。
- 由此**复活了节点级 `llm_config` 死特性**（此前只写库不读回）。
