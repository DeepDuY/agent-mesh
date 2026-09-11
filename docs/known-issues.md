# 已知问题与后续演进

> 来源：原 DESIGN.md §18。

1. ~~**MCP SSE 无鉴权**~~ 已修复：`_run_sse_server` 自建 Starlette app + Bearer 鉴权中间件，仅接受用户 token（见 [protocol.md](./protocol.md)、[auth-security.md](./auth-security.md)）。
2. ~~**admin 密码每次启动强制复位为 `admin`**~~ 已修复：`_ensure_admin_user` 仅在占位/非法 hash 时重置；admin 可自助改密。新增 admin token 首次引导（日志打印一次）。
3. **`depends_on` 仅校验存在、未持久化/未阻塞执行**：依赖任务未完成时派发不会等待。
4. ~~**节点级 `llm_config` 是死特性**~~ 已修复：`_row_to_agent` 读回 `agents.llm_*`，节点级配置通过 LLM 配置同步（见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)）随心跳下发，优先级高于全局默认。
5. ~~**产物元数据异步落库**~~ 已修复：改为 `await` 同步落库（`INSERT OR IGNORE` 幂等兜底不变）。
6. **`task_events` 表已无写入方**：审计接口后续可基于它实现。
7. ~~**`GET /api/bootstrap/install.sh` 内嵌全局 LLM apiKey 明文**~~ 已修复：外层安装脚本不再内嵌任何 LLM 配置（`api/bootstrap.py`），新节点注册后由心跳 config-sync 下发 LLM 配置（见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)），安装脚本与进程列表不再出现任何凭据。`os/arch` 查询参数未校验（仅影响下载文件名）。
8. ~~**`tests/e2e.py` 目标错误**~~ 已移除：该文件（连同遗留的 `edge/mcp_client.py`）已删除，探针纯走 REST，不再引用 MCP 客户端。
9. Web 看板为静态轮询（5s），后续可升级 WebSocket。10. Windows 边沿安装尚未支持。
11. 后续支持按用户生成 MCP skills（每个 skill 携带用户 token）；增强审计与任务日志。
12. ~~**规划中：agent 自升级机制**~~ 已实现：`POST /api/agents/{id}/upgrade`（或看板「升级」按钮）→ 心跳下发 `upgrade` 指令（版本 + 包名）→ edge 空闲时从 `/api/bootstrap/{pkg}` 下载 → staging 校验后原子替换 `bin/agent-mesh-edge.bin`（保留 `.bin.old` 与 `agent_version.bak`）→ 写入 rollback-aware wrapper 与 `etc/upgrading` 标记 → `execv` 重启。回滚采用两阶段握手：首次重启时 wrapper 记录 `etc/upgrade-started` 并运行新二进制；新二进制首次成功心跳后由 edge 清除标记与备份（升级确认健康）；若新二进制启动失败、systemd 再次拉起 wrapper 时发现 `upgrading` + `upgrade-started` 同时存在则自动回滚 `.bin.old` 并恢复旧版本号。新增 `agents.version/arch/upgrade_requested/upgrade_version/upgrade_requested_at` 列（迁移 007），`bootstrap_download` 改为 `require_any_token` 以便全局 token 的边沿下载安装包。详见 [auth-security.md §8](./auth-security.md#8-agent-自升级)。
13. ~~**规划中：LLM 配置同步**~~ 已实现：看板/`PATCH /api/settings` 保存 `llm_*` 或 `PATCH /api/agents/{id}/llm_config` 时 `config_version` 自增 → 心跳响应携带 `config_version` 与解析后的 `config`（全局默认，节点级 llm_config 优先）→ edge 对比本地 `etc/config_version`，变更时更新 executor、重写 `edge.env` 并落盘版本号。新增 `config_version` setting（迁移 007）。详见 [auth-security.md §9](./auth-security.md#9-llm-配置同步)。
14. **agent 独立 token 机制已实现**（见 [auth-security.md §7](./auth-security.md#7-agent-独立-token-与设备-用户关联)）：首次注册自动签发、轮换接口、token 绑定 device_id、`agent_users` 设备-用户关联表（迁移 008）。**多用户权限管控尚未实现**：`agent_users.allowed_users` 目前只记录，派发/取消/删除任务时尚未按 allowed_users 校验——admin 默认全权，普通用户操作任意节点的权限管控需后续实现。旧二进制（无 token 持久化逻辑）不重装则只能继续用全局/用户 token。
15. **2026-08-17 线上故障记录（LLM 任务全部失败 + 排查与修复）**：
    - **表象**：LLM 任务全挂，command 正常。`list_agents` 的 `llm_*` 为 null 被误判为"节点缺配置"（实际那三列是节点级覆盖字段，全局走 settings + 心跳 `config`，与它无关）。
    - **根因①（真 bug）**：`build_opencode_config()` 生成的 opencode `models` map 用了带前缀的 key（`lite/deepseek-v4-flash`、`vip/deepseek-v4-pro`），而网关真实模型是 `deepseek-v4-flash`、`deepseek-v4-pro`（无前缀）。探针 per-task 配置里模型解析失败 → 网关 404/5xx。198 一直能跑只是因为 root 家目录有份手动全局 `~/.config/opencode/opencode.jsonc` 补对了 map。**修复**：`models` map 改为网关真实 id，并 `_canonical_model_id()` 去 `lite/`/`vip/` 前缀后 setdefault（见 `config_writer.py`）。
    - **关键差异**：per-task 配置（`OPENCODE_CONFIG` env 指向的 opencode.json）下，opencode **原样发送**模型 id（不剥前缀）：`anthropic/deepseek-v4-flash` 正常、`anthropic/lite/deepseek-v4-flash` 会 404（实测）；而全局配置下 opencode 会剥 `lite/`。所以 settings `llm_model` 必须填**网关真实存在的完整模型名**：裸模型 `anthropic/deepseek-v4-flash`，或带前缀但真实存在的 `anthropic/vip/kimi-k2.7-code`（实测均可用）；填不存在的（如 `vip/deepchat`）会 404/5xx。
    - **根因②（真 bug）**：自升级 `_perform_upgrade` 用 `shutil.copy2(new_bin, bin_file)` **原地覆盖正在运行的二进制** → Linux `Text file busy`，升级永远失败并每轮重新下载 87MB。**修复**：改用 `os.replace()`（先写 `.bin.new` 暂存再 rename 原子替换，见 `edge/agent.py`）。
    - **根因③（部署坑）**：FORCE_REINSTALL 重装不会清 `etc/config_version`，新二进制启动时本地版本==服务端版本 → 跳过配置同步 → 继续用 install.sh 写入的旧 model。**修复**：install.sh 重装时清理 `etc/config_version`/升级标记/`.bin.old`（见 `data/bootstrap/install.sh`）。
    - **处置**：正确模型 = `anthropic/deepseek-v4-flash`（config_version=4）；12/14/15/16 已到 1.2.0 并 LLM 全部跑通。**node 13（device `<redacted>`）当时自 08:23 离线需到机器排查**（非本次操作导致；该节点后续已恢复并升级，现在线 v1.4.2）。
    - **快照说明**：本段为 2026-08-17 现场状态（当时节点版本、模型结论均属当时），模型/离线/版本结论勿当作当前现状引用；当前版本见 [evidence-02](./evidence-02-production-run-records.md) §3。
16. **v1.4.0 新增：多任务并发执行 + LLM 实时执行输出**：
    - **多任务并发**：全局设置 `max_concurrent`（默认 2，配置页可调），心跳响应下发；边沿异步并发执行（`asyncio.create_task`），未显式 workdir 的任务落到独立子目录；`poll_for_task` 响应扩展 `tasks` 数组（保留 `task` 兼容）。SQLite `dequeue` 原子化为 `DELETE ... RETURNING`。
    - **实时输出**：边沿解析 opencode JSONL 事件流（text/error/complete/raw），批量 `POST /api/edge/task_log` → `task_logs` 表（迁移 012）；`GET /api/tasks/{id}/logs?after_id=` 增量查询，看板任务详情实时轮询展示。
    - **注意**：旧版探针（<1.4.0）不发送 `running_tasks`、只读 `task` 字段，orchestrator 按单任务串行兼容；新版探针才享受并发与实时输出。
17. **v1.4.0 存储层重构：统一数据库对接层 + 生产切换 PostgreSQL**：
    - **新增 `store/connection.py`**（统一连接层）：`Database` 抽象 + `SQLiteDatabase` + `PostgresDatabase`，集中管理两侧连接生命周期与执行原语；SQL 统一 `?` 占位符（PG 层内部转 `$n`）；SQLite 连接从 `sqlite/connection.py` 迁入。
    - **修复 PG 后端隐藏 bug**（此前 PG 后端从未真实跑通）：① `main.py` 用 `asyncio.run(_init())` 独立 loop 建 asyncpg pool、后续服务器 loop 复用 → 跨 loop 崩溃；② uvicorn 多进程 fork 后 worker 继承主进程 pool → 崩溃；③ `task_results` 缺 `session_id` 列；④ `get_user_by_username`/`get_user_by_token_hash` SELECT 漏 `password_hash`；⑤ PG `TIMESTAMP` 列要求 naive datetime（`_iso_to_dt` 对 naive 加 UTC、`_pg_param` 把 aware 归一化为 naive）。
    - **多进程支持**：`PostgresDatabase._acquire()` 按 `os.getpid()` 惰性建池，fork 的 worker 自动重建各自连接池——**无需强制单进程**，8 worker 生产照常。
    - **生产切换**：`orchestrator.env` 加 `AGENT_MESH_DB_TYPE=pg` + `AGENT_MESH_PG_DSN`；数据经 `scripts/migrate_sqlite_to_pg.py` 迁移（users/agents/tasks/results/artifacts/files/skills/settings/logs 全量，序列同步）。SQLite DB 保留 `.db.bak.pg-migrate-*` 可回滚。
18. **2026-09-01 P0 代码审查修复**（[../code-review-report.md](../code-review-report.md) 高优先级项，全部修复并通过 90 项回归测试）：
    - **`edge/execution/command.py` 重复等待**：`run_command` 已 `await _wait_proc` 并进入 `finally` 清理 `log_task`，随后又重复调用 `_wait_proc`（第二次不再读取输出、重复创建 wait/cancel 任务）。已删除第二次调用。
    - **空 `session_secret` 可签发/伪造 session token**：登录时 `secret or ""` 会用空 secret 签发。已改为空 secret 直接 500（fail-closed）；`verify_session_token()` 对空 secret 一律返回 `None`。`_ensure_session_secret` 仍保证首启生成。
    - **`_sweep_timeouts` 非原子、可能覆盖真实结果**：先 `get_task` 判断再写结果/置 `timed_out`，期间若边沿提交结果会被覆盖。已为 `update_task_status` 增加 `expected_status` 条件参数（SQLite/PG 双后端实现），清扫器先原子 `working→timed_out`，失败（任务已终态）即跳过、不写结果。
    - **`scripts/start-orchestrator.sh` 硬编码 `AGENT_MESH_TOKEN=demo-token`**：改为从 `data/orchestrator.env` 读取；缺失/占位/默认值时首启自动生成随机 token 并持久化。
    - **Web 登录框预填 `admin/admin`**：已移除默认值，强制手动输入。
    - **看板配置保存不校验响应**：`patchSettings()` 校验 `res.ok`，失败显示具体错误（不再无条件显示"已保存"）；各保存入口接入 try/catch 错误提示。
    - **登录后用户标签不更新**：`app.js` 的 `const USER` 改为登录后从 `localStorage` 动态读取。
    - **edge 探针版本升级至 v1.4.1**：因 `command.py` 改动属探针代码，按变更规范升 `shared/constants.py:VERSION` 并重建安装包（`data/bootstrap/agent-mesh-agent-linux-x64.tar.gz`，含 VERSION=1.4.1）；在线节点空闲时自动升级。
    - **构建脚本修复**：`build-agent-bootstrap.py` 的 `_build_binary` 由 `uv run pyinstaller` 改为 `sys.executable -m PyInstaller`（避免 uv 重新解析环境）；构建命令改用 `uv run --no-sync python scripts/build-agent-bootstrap.py`——本机 glibc 2.17 下 asyncpg 0.31.0 的 `manylinux_2_28` wheel 不可用，`uv run` 会回退源码编译而失败（venv 实际装的仍是可用的 0.29.0）。
19. **2026-09-01 SQLite→PG 切换窗口数据分叉（技能/文件丢失）**：
    - **表象**：看板「技能」页少 2 个技能（`agent-compose`、`octobus`），「文件」页少 2 个文件（`f-46071ec5`、`f-ee0aa88a`，均为 `SKILL.md`）。
    - **根因**：时间线错位。PG 迁移在 08-31 06:38 执行（`agent-mesh.db.bak.pg-migrate-20260831-063829` 仅含当时的 2 技能/35 文件）；随后 06:40-06:41 上传的 2 技能 + 2 文件写入的是**仍在用的 SQLite**（`/opt/agent-mesh/data/agent-mesh.db`）；直到 08-31 10:02 才把 `orchestrator.env` 改为 `AGENT_MESH_DB_TYPE=pg` 并重启。此后看板读 PG，故迁移后、切库前产生的数据永久停留在 SQLite，PG 缺失。
    - **影响**：PG 多出 13 个任务（切换后新增，正常）；skills 少 2、files 少 2。经查 PG 无任务引用这两个 file_id，且磁盘文件均在。
    - **修复**：从 SQLite 元数据 + 磁盘 zip/文件，将 2 技能 + 2 文件按**原 file_id** 直接插入 PG（备份 `/opt/agent-mesh/data/skills-backup-20260901.json`）。API 验证 skills=4、files=37，SKILL.md 下载 200。
    - **预防**：`migrate_sqlite_to_pg.py` 迁移后、切换 `db_type=pg` 前的空窗期内，任何写入都只进 SQLite。切换后应**先做一次 SQLite→PG 增量同步**（或禁用写），确认两边计数一致后再切。
20. **2026-09-01 任务/文件管理增强（纯 orchestrator 端，无需升级探针）**：
    - **任务查询扩展**：`GET /api/tasks` 新增 `mode`(command/llm)、`search`(指令/任务ID/节点模糊 LIKE/ILIKE)、`started_after`/`started_before`(ISO 开始时间区间，可组合实现"之后/之间/之前")；`count_tasks` 同步支持。批量删除 `all_matching` 模式同步支持上述筛选。
    - **文件管理扩展**：`GET /api/files` 新增 `search`(文件名/文件ID 模糊)；新增 `POST /api/files/batch-delete`（DB+磁盘批量清理）、`POST /api/files/batch-download`（打包 ZIP，重名自动加 file_id 前缀）。
    - **Web 看板**：任务页由状态按钮改为 搜索框 + 状态下拉 + 模式下拉 + 开始时间区间（之后/之间/之前）组合筛选；文件页新增搜索框、复选全选、批量删除/批量下载。
    - **实现说明**：`AbstractStore.list_tasks/count_tasks/list_files` 签名扩展（SQLite/PG 双后端同步）；SQLite 时间比较用 `_dt_to_iso` 字符串、PG 用 naive UTC timestamp。90 项回归测试通过，生产 API 实测验证。
21. **2026-09-01 文档-代码一致性审计 + PG 原子更新高危 bug**：
    - **高危（已修）**：`store/pg.py` 的 `update_task_status` 传 `expected_status` 时参数错位——`expected_status` 被追加进 **SET 子句**、`task_id` 被绑定到 `WHERE task_id` 占位符（`SET status=task_id`、`WHERE task_id=expected_status`）。因 `WHERE task_id='working'` 匹配不到行，PG 部署下超时清扫**静默失效**（任务永不超时/永不重试）。**修复**：改为 `WHERE task_id = ? AND status = ?` 追加条件（与 SQLite 一致）；新增 `tests/test_pg_atomic.py` 回归测试（92 项全通过），生产已部署。
    - **文档过时修正**：MCP 工具数 "11 个" → "12 个"（`orchestrator.md` §1.2、`architecture.md` 架构图；含 `get_task_logs`/`list_skills`）；`protocol.md` `/api/bootstrap/install.sh` 仍写"内嵌 LLM 全局配置"→ 改为"不再内嵌"；两处 sqlite 目录树补 `files.py`/`logs.py`/`skills.py`；`deployment.md` wrapper 的 `staging/.upgrade-ready` 过时标记 → 改为实际的两阶段 `etc/upgrading` + `etc/upgrade-started` 机制。
22. **2026-09-11 多用户（P0-A）+ 移除 MemoryStore**：
    - **Web 看板用户管理**：新增「用户」tab（仅 admin 可见）与「修改密码」入口，补齐了此前只有 REST 接口、看板无入口的局面。新增 `web_ui/js/users.js`（列表/创建/删除/重置密码/轮换 token，一次性 token 弹窗展示）；`auth.js` 登录后持久化 `role`；`app.js` 增加 `isAdmin()`/`applyRole()`/`refreshMe()`，普通用户自动隐藏 admin 专属元素。后端接口此前已具备，未改动。
    - **移除 `store/memory.py`（`MemoryStore`）**：它只是测试夹具，**并非运行时存储**（`config.db_type` 仅 `sqlite`/`pg`）。同步新增 `tests/conftest.py`（临时 SQLite 文件 + `sqlite_client` 辅助、固定 admin token），9 个测试模块改用 SQLite；`test_auth_api` 的禁用用户用例改为直接 `UPDATE users SET disabled=1`。删除 2 个与 SQLite 用例完全重复的 MemoryStore 遥测用例。回归 **90 项全通过**。文档目录树/描述同步去掉 memory。
    - **顺带修复真 bug**：SQLite `delete_task` 未删除 `task_logs`（migration 012 无 FK 级联），单任务删除会残留日志（PG 侧已有删除，`delete_tasks` 也已删）。已在 `store/sqlite/tasks.py:delete_task` 显式清理，`test_delete_task_cascades_logs` 覆盖。
23. **2026-09-11 方向二：个性化 agent 模板（节点描述 + 提示词 + 模型可配置）**：
    - **模型列表去硬编码**：新增 `settings.llm_models`（配置页「可用模型列表」，每行一个完整网关 id；迁移预置现有 3 个）。edge `build_opencode_config()` **删除写死的 `models` map**，改为按该列表动态生成（键=去 `anthropic/` 前缀、`name`=末段；请求模型 `setdefault` 兜底）；列表经心跳 config 同步（`_resolve_edge_config` / MCP `poll_for_task`），edge 持久化到 `etc/llm_models`。
    - **强制配置（去掉所有默认模型兜底）**：`EdgeConfig.llm_model`、`api/edge.py` 的 `_DEFAULT_LLM_MODEL`、MCP poll 兜底、migration 004 / PG `_ensure_settings` 的 `llm_model` 种子全部清空。`mode=llm` 且无模型时 REST 派发 400、MCP `accepted:false`、edge `run_llm` fail-fast；`Constraints.model` 显式指定且 `llm_models` 非空时须命中列表。`run_llm` 传 `llm_models` 给 `build_opencode_config`。
    - **节点描述**：`agents.description`（迁移 013；`AgentStatus` 暴露，REST/MCP 可见），`PATCH /api/agents/{id}/description` + 节点详情弹窗编辑；主 Agent 通过 `list_agents` 识别节点用途。
    - **节点级 system prompt**：`agents.system_prompt`（节点级，后被 §24 模板机制取代全局默认；全局 `settings.system_prompt` 已移除），经 config-sync 下发；edge 持久化到 `etc/system_prompt`（多行安全）并注入 `_wrap_llm_instruction(instruction, system_prompt)` 顶部的「## 角色与上下文」块。`PATCH /api/agents/{id}/system_prompt` + 节点详情弹窗编辑。
    - **MCP**：新增 `list_models` 工具返回可用模型；`list_agents`/`get_agent` 带出 `description`/`system_prompt`。
    - **探针**：`shared/constants.py:VERSION` 升 **1.5.0**（edge 代码变更），需重建 bootstrap 并让在线节点自动升级；旧节点不注入 prompt、仍用自身硬编码 map，功能不受影响。
    - **测试**：新增 `tests/test_agent_profile.py`（描述/提示词、config-sync、`apply_llm_config` 持久化、`build_opencode_config` 无硬编码、`_wrap_llm_instruction` 注入、模型白名单/强制配置），共 **99 项全通过**。
24. **2026-09-11 节点模板（可复用配置 + 引用式绑定）**：
    - **背景**：方向二把提示词/模型都挂在单个节点上，难复用、难批量管理。新增独立的**模板**模块，作为后续节点权限等配置的统一载体。
    - **数据模型**：`templates(id, name UNIQUE, description, system_prompt, llm_model, allowed_tools JSON, data JSON, created_at, updated_at)`；`agents.template_id`（引用式绑定，`ON DELETE SET NULL`）。迁移 `014_templates.sql`；PG 侧在 `_ensure_agent_schema_upgrade` 建表/加列。
    - **生效规则**：模型 `节点 llm_model > 模板 llm_model > 全局 settings.llm_model`；提示词 = `内置 wrapper + 节点 system_prompt + 模板 system_prompt`（节点在前）。**移除全局 `settings.system_prompt`**（迁移 014 / PG 启动时删除该行；配置页去掉对应输入框）。
    - **API**：`GET/POST /api/templates`、`GET/PATCH/DELETE /api/templates/{id}`、`PATCH /api/agents/{id}/template`（绑定/解绑）。模板任何改动都 `bump_config_version` → 所有绑定节点下一次心跳自动重拉。
    - **Web**：新增「模板」tab（列表/新建/编辑/删除，显示绑定节点数）；节点详情新增「应用模板」下拉 + 显示当前模板。
    - **MCP**：无新增工具（模板管理走 REST/Web）；`agents.template_id` 随 `AgentStatus` 暴露。
    - **测试**：新增 `tests/test_templates.py`（CRUD、名称校验/重名、绑定/解绑、删除模板自动解绑），全量 **103 项通过**。

---

## 下一步开发方向（路线图，2026-09-01 规划）

### 方向一：edge-agent 执行权限管控
- **目标**：让边沿节点上的命令/工具执行受到可配置、可审计的权限约束。
- 任务级 `allowed_tools` / `permission` 真正下发生效（当前 llm 模式经 opencode permission 部分生效；**command 模式 `bash -c` 无任何工具限制**）。
- command 模式指令白名单/风险校验（危险命令阻断、可配置允许/拒绝规则）。
- `Constraints.skills` 任务级技能提示落地（schema 已有字段，edge executor 未读取）。
- 基于 `task_events` 表补执行审计（当前无写入方，预留接口）。

### 方向二：个性化 agent 模板（llm 提示词 + 节点描述 + 模型选择）—— ✅ 2026-09-11 完成（§23）
- ~~**LLM 提示词拼接**：节点级 system prompt 模板~~ 已实现：节点级 `agents.system_prompt` + 全局 `settings.system_prompt`，config-sync 下发，edge 注入 `_wrap_llm_instruction()` 顶部。
- ~~**节点描述主 agent 可见**：`AgentStatus` 增加 `description`~~ 已实现：`agents.description` + `PATCH /api/agents/{id}/description`，MCP `list_agents`/`get_agent_detail` 返回。
- ~~**edge-agent 的 LLM 模型可选其他模型**~~ 已实现：`Constraints.model` 任务级下发 + `settings.llm_models` 白名单校验 + MCP `list_models`；模型列表去硬编码、由配置页维护；无默认兜底（强制配置）。
- 说明：**任务级 system prompt 未单独加字段**（任务自身的 `instruction` 即任务级内容，需角色/上下文直接写进指令）。

### 补充建议（同优先级，按价值排序）
1. **多用户权限管控落地**（关联方向一）：`agent_users.allowed_users` 目前只记录不校验，按用户限制派发/取消/删除任务（§14 已预告）。
2. **删除任务时清理 artifact_store 磁盘产物**：当前只删 DB 记录，磁盘文件残留。
3. **任务依赖 `depends_on` 落地**：当前仅校验存在、未持久化/未阻塞执行。
4. **WebSocket 实时推送**：替代看板 5s 静态轮询（§9 已预告）。
5. **`GET /api/bootstrap/install.sh` 的 `host` 注入校验**：当前未校验格式（代码审查 §4.2 遗留，中风险）。
6. **MCP skills 按用户生成**：每个 skill 携带用户 token（§11 已预告）。
7. **看板「任务详情/操作」错误提示补全**：`agents.js`/`tasks.js`/`files.js`/`skills.js` 多处操作仍未校验响应状态（仅 `config.js` 已修）。

### 远期规划（较远实现，低优先级）
- **Windows 边沿探针支持**（§10 遗留）：当前仅 Linux（主）/macOS（darwin 部分），Windows 只在上报字段预留 `win32`，无安装包/无法运行。实现需覆盖：
  - `scripts/build-agent-bootstrap.py` 增加 win32 目标（PyInstaller Windows 构建，产出 `agent-mesh-agent-win32-x64.zip`/`.tar.gz`，含 `opencode.exe`）。
  - 安装：新增 `install.ps1`（替代 bash `install.sh`），注册 Windows 服务（`sc.exe` / NSSM）或计划任务自启；wrapper 改为 `.cmd`/`.ps1` 兼容两阶段升级回滚。
  - 命令执行：`command.py` 的 `bash -c` 适配（探测 `bash`/Git Bash/WSL，或改用 `cmd /c` / PowerShell），`_terminate_proc` 信号语义（Windows 无 SIGTERM 等价）。
  - 自升级：`_perform_upgrade` 放开 win32 分支（当前直接拒绝），服务重启逻辑适配（`sc.exe stop/start` 或 `Restart-Service`）。
  - 信号处理：`loop.add_signal_handler` 在 Windows 的兼容（SIGTERM/SIGINT 差异）。
  - `get_device_id`/`get_arch`/`get_distro` 的 Windows 实现（machine-id 改用注册表/`HKLM`，arch 用 `PROCESSOR_ARCHITECTURE`）。
  - 端到端验证：Windows 节点注册→心跳→command/llm 任务→产物上传→自升级。

> 实施顺序建议：先做"可快速见效"的 2/5/7（小改动），再推进方向一的权限管控底座（allowed_tools 下发 + command 白名单），方向二按 节点描述→提示词模板→任务级模型 的顺序叠加；Windows 支持整体置于路线图最后，作为远期目标。
