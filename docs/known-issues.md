# 已知问题与后续演进

> 本文档只记录**当前仍未解决**的问题与**待推进**的方向。
> 已修复的历史变更记录（含早期故障排查、整改批次）已从本文移除，需要时查 git 历史（`git log -p docs/known-issues.md`）。

## 1. 未解决问题

### 架构 / 存储

1. **多进程未真正生效**：`AGENT_MESH_WORKERS>1` 不起作用——`main.py` 用 `uvicorn.Config(workers=N)` 配合 `uvicorn.Server(config).serve()`，该版本 uvicorn 的 `Server.serve()` 忽略 `workers`，实际只跑 1 个 server 进程；`workers` 默认值 `os.cpu_count()` 与"单进程运行"也不一致（建议默认 `1`）。真正多 worker 需改用 import-string + `uvicorn.run`（或 supervisor），并解决 lifespan 可 pickle、sweeper 归属。
   - **连带约束**：实时推送（`/api/realtime` SSE）目前是**进程内**广播（`orchestrator/realtime.py`），只在单 worker 下正确；一旦启用多 worker，必须改成共享总线（Postgres `LISTEN/NOTIFY`）。
2. **heartbeat 容量检查非原子**：单进程可加锁；PG 多 worker 下"统计活跃数 → dequeue 补足 capacity"需放到 DB 级事务里，否则并发心跳会超额派发。
3. **agent 键解析不统一**：队列键（`device_id or agent_id`）与 `agents` 行定位（数字 id → device_id → 显示名）语义分散，`alias`/`hostname` 显示名并未真正参与解析（见 §1 协议 `agent_id` 三级解析的文档口径）。
4. **SQLite→PG 迁移脚本不完整**：`scripts/migrate_sqlite_to_pg.py` 未覆盖 `teams`/`team_members`/`templates`/`task_events` 与 `tasks.user_id/team_id`；迁移 015 后仍读取已删除的 `templates.allowed_tools` 列会报错。
5. **超时重试状态翻转语义**：`_sweep_timeouts` 的回退重试与状态机语义需与容量检查原子性（§2）一并设计。

### 功能 / 运维

6. **生产 orchestrator 日志文件缺失**：systemd 配置 `StandardOutput=journal`，`log/orchestrator.log` 从不生成，但文档与 `deploy/status.sh` 仍按该文件 tail。需二选一：重定向写入该文件，或改文档 + 脚本。
7. **SQLite 仍残留全局 `settings.system_prompt`**：迁移 013 会 seed，PG 启动时会删除；代码已不再读取该行，属死数据，应在 SQLite 侧清理。
8. **SSE 推送覆盖面有限**：目前只推任务状态（`tasks_changed`）、任务日志（`task_log`）、节点上下线（`agents_changed`）；文件/技能/模板/配置仍靠 5s 轮询。

### 平台

9. **macOS Intel（darwin-x64）无探针包**：CI 只构建 `darwin-arm64`；`install.sh` 会按 `uname` 找 `darwin-x64` 但发布里没有。
10. **Windows 节点不支持自升级**：服务端对 `win32` 跳过自动升级指令（安装/重装正常）；需实现计划任务下的下载替换 + 重启。
11. **探针 Release 自动同步缺失**：目前只有配置页手动按钮（`POST /api/bootstrap/sync`），无启动/定时/发布回调触发。

### 执行隔离后续

12. **`Constraints.skills` 未落地**：schema 已有字段，edge executor 未读取；任务级技能提示未生效。
13. **缺强隔离沙箱 / 结构化 argv**：command 权限匹配器（OpenCode permission 规格）**只防误操作、不是安全边界**，shell 间接调用可绕过；生产建议低权限账号/容器。

---

## 2. 下一步开发方向

### 方向一：edge-agent 执行权限管控（第一期已完成）

统一权限模板（`templates.permission`，OpenCode `permission` 规格，llm 与 command 共用）、command 白名单、`task_events` 审计均已落地。**未做**：强隔离沙箱 / 结构化 argv（§13）、`Constraints.skills` 任务级技能提示落地（§12）。

### 补充建议（按价值排序）

1. **安全收口第二期**：`GET /api/settings` 已限 Web UI + 白名单；节点详情已对非 admin 脱敏；文件下载已按任务归属限制 agent token。后续可评估全局 token 的下载范围、以及 LLM 密钥的写入/回显策略。
2. **`depends_on` 已落地**：依赖全部 `completed` 才派发；依赖失败/终止会级联取消依赖者；派发时校验环。后续可加"等待依赖"的显式状态/UI 提示。
3. **多进程与心跳原子性**（§1/§2），并随之把实时推送换成 `LISTEN/NOTIFY` 总线。
4. **平台补齐**：macOS Intel 包、Windows 自升级、探针 Release 自动同步（§9/§10/§11）。
