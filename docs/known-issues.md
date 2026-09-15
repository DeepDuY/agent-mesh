# 已知问题与后续演进

> 本文档只记录**当前仍未解决**的问题与**待推进**的方向。
> 已修复的历史变更记录（含早期故障排查、整改批次）已从本文移除，需要时查 git 历史（`git log -p docs/known-issues.md`）。

## 1. 未解决问题

### 架构 / 存储

1. **多进程未真正生效**：`AGENT_MESH_WORKERS>1` 不起作用——`main.py` 用 `uvicorn.Config(workers=N)` 配合 `uvicorn.Server(config).serve()`，该版本 uvicorn 的 `Server.serve()` 忽略 `workers`，实际只跑 1 个 server 进程；`workers` 默认值 `os.cpu_count()` 与"单进程运行"也不一致（建议默认 `1`）。真正多 worker 需改用 import-string + `uvicorn.run`（或 supervisor），并解决 lifespan 可 pickle、sweeper 归属。
2. **heartbeat 容量检查非原子**：单进程可加锁；PG 多 worker 下"统计活跃数 → dequeue 补足 capacity"需放到 DB 级事务里，否则并发心跳会超额派发。
3. **agent 键解析不统一**：队列键（`device_id or agent_id`）与 `agents` 行定位（数字 id → device_id → 显示名）语义分散，`alias`/`hostname` 显示名并未真正参与解析（见 §1 协议 `agent_id` 三级解析的文档口径）。
4. **SQLite→PG 迁移脚本不完整**：`scripts/migrate_sqlite_to_pg.py` 未覆盖 `teams`/`team_members`/`templates`/`task_events` 与 `tasks.user_id/team_id`；迁移 015 后仍读取已删除的 `templates.allowed_tools` 列会报错。
5. **超时重试状态翻转语义**：`_sweep_timeouts` 的回退重试与状态机语义需与容量检查原子性（§1）一并设计。

### 安全

6. **`GET /api/settings` 暴露敏感字段**：该端点（`require_admin`）返回 `store.list_settings()` 的**全量**键值，包含 `session_secret`（session token 签名密钥）。应对返回字段做白名单（`public_url`/`llm_*`/`llm_models`/`default_permission`/`auto_upgrade` 等），并单独决定是否给 admin 暴露 `agent_mesh_token`。
7. **节点详情泄漏节点级 LLM 凭据**：`GET /api/agents/{id}` / `detail` 对可访问该节点的**非 admin** 用户返回 `llm_api_key`/`llm_base_url`/`llm_model`（`_dump_agent` 仅剥离 `access`）。应对非 admin 脱敏。
8. **`GET /api/files/{file_id}` 任何 agent token 可下载任意文件**：仅按身份放行，未按任务归属校验（`api/files.py` 对 `auth != "user"` 跳过归属检查），与"agent 仅能访问自身任务产物"的隔离目标不一致。

### 功能 / 运维

9. **`depends_on` 仅校验存在**：依赖任务未完成时派发不会等待（既不持久化、也不阻塞执行）。
10. **删除任务不清理磁盘产物**：`artifact_store` 只删 DB 记录，磁盘文件残留。
11. **`GET /api/bootstrap/install.sh` 的 `host` 注入未校验格式**；`/api/bootstrap/{filename}` 与 `os/arch` 查询参数也未校验（仅影响下载文件名）。
12. **command 模式实时日志只有 stdout**：`edge/execution/command.py` 未给 `proc.stderr` 挂回调，stderr 行不会作为 `raw` 上报（LLM 模式的 error 走 JSONL stdout，不受影响）。
13. **生产 orchestrator 日志文件缺失**：systemd 配置 `StandardOutput=journal`，`log/orchestrator.log` 从不生成，但文档与 `deploy/status.sh` 仍按该文件 tail。需二选一：重定向写入该文件，或改文档 + 脚本。
14. **SQLite 仍残留全局 `settings.system_prompt`**：迁移 013 会 seed，PG 启动时会删除；代码已不再读取该行，属死数据，应在 SQLite 侧清理。
15. **Web 看板为静态轮询（默认 5s）**，可升级 WebSocket。
16. **看板操作错误提示不全**：`agents.js`/`tasks.js`/`files.js`/`skills.js` 多处未校验响应状态（`config.js` 已修）。

### 平台

17. **Windows 边沿探针未支持**：仅 Linux（主）/macOS（部分）；实现需覆盖构建、`install.ps1`、服务注册、`bash`/命令执行适配、自升级、`get_device_id`/`get_arch`/`get_distro` 与端到端验证。

### 执行隔离后续

18. **`Constraints.skills` 未落地**：schema 已有字段，edge executor 未读取；任务级技能提示未生效。
19. **缺强隔离沙箱 / 结构化 argv**：command 权限匹配器（OpenCode permission 规格）**只防误操作、不是安全边界**，shell 间接调用可绕过；生产建议低权限账号/容器。

---

## 2. 下一步开发方向

### 方向一：edge-agent 执行权限管控（第一期已完成）

统一权限模板（`templates.permission`，OpenCode `permission` 规格，llm 与 command 共用）、command 白名单、`task_events` 审计均已落地。**未做**：强隔离沙箱 / 结构化 argv（§19）、`Constraints.skills` 任务级技能提示落地（§18）。

### 补充建议（按价值排序）

1. **删除任务时清理 `artifact_store` 磁盘产物**（§10）。
2. **`depends_on` 落地**：持久化 + 阻塞/触发执行（§9）。
3. **WebSocket 实时推送**：替代看板静态轮询（§15）。
4. **`GET /api/bootstrap/install.sh` 的 `host` 注入校验**（§11）。
5. **看板「任务详情/操作」错误提示补全**（§16）。
6. **安全收口**：`GET /api/settings` 字段白名单（§6）、节点详情 LLM 凭据脱敏（§7）、文件下载按任务归属校验（§8）。

### 远期规划（低优先级）

- **Windows 边沿探针支持**（§17）。
