# REST 接口规范

## 边界和命名

- 控制面以 `/api` 为前缀；资源使用复数、小写连字符，JSON 字段与动作名使用 `snake_case`。设备心跳 `/api/edge/*` 属独立探针协议，安装脚本、二进制下载和 SSE 属传输接口，不适用资源动作命名。
- 集合：`GET /resources` 列表，`POST /resources` 创建；单项：`GET/PATCH/DELETE /resources/{id}`。部分更新必须用 Pydantic 模型及 `model_fields_set` 区分省略和显式置空。
- 独立的子集合（日志、事件、附件）使用 `/resources/{id}/subresources`。
- 非 CRUD 动作：`POST /resources/{id}:action`；批量动作：`POST /resources:batch_action`，例如 `POST /tasks/{id}:cancel`、`POST /tasks:batch_delete`。路径中 `:` 为字面量；动态 ID 匹配到最后一个动作后缀，测试须覆盖包含冒号的设备 ID。
- 列表保留 `limit`、`offset`、`total` 与当前响应字段；错误维持 HTTP 状态码及 FastAPI `detail`，避免凭空引入与存量 SDK 不兼容的包络。新增错误应明确 400/401/403/404/409，不以 200 代替失败。
- 动作必须在处理前核验调用者角色、租户与资源状态；合并 PATCH 时按字段鉴权，不因字段进入同一接口降低权限。
- 写入类请求支持 `Idempotency-Key` 时，必须定义调用者作用域与并发语义；时间输出带 UTC 时区偏移。文件、产物、SSE、安装脚本继续使用 HTTP 流式响应。

## 控制面映射

| 资源 | 创建/查询/编辑 | 动作与集合操作 |
|---|---|---|
| 任务 | `POST /tasks`、`GET /tasks`、`GET/DELETE /tasks/{id}`；`/events`、`/logs`、`/reviews` 为子资源 | `POST /tasks/{id}:cancel`、`:approve`、`:reject`、`POST /tasks:batch_delete` |
| 批次 | `POST /runs`（创建批次并派发）、`GET /runs/{id}` | `POST /runs/{id}:cancel`、`:retry` |
| 节点 | `GET /agents`、`GET/PATCH/DELETE /agents/{id}`（`?include=tasks` 返回详情） | `POST /agents/{id}:rotate_token`、`:upgrade`、`POST /agents:batch_update` |
| 用户 | `POST/GET /users`、`DELETE /users/{username}`、`GET /me` | `POST /users/{username}:rotate_token`、`:reset_password`、`POST /me:rotate_token`、`:change_password`、`POST /auth:login` |
| 文件 | `POST/GET /files`、`GET/DELETE /files/{id}` | `POST /files:batch_delete`、`:batch_download` |
| 计划 | `POST/GET /schedules`、`GET/PATCH/DELETE /schedules/{id}` | `POST /schedules/{id}:run` |
| 节点产物 | `POST/GET /tasks/{id}/artifacts`、`GET /tasks/{id}/artifacts/{artifact_id}` | — |
| 探针分发 | `GET /bootstrap/info`、`GET /bootstrap/install.sh`、`GET /bootstrap/install.ps1`、`POST /bootstrap/packages`、`GET /bootstrap/packages/{filename}` | `POST /bootstrap:sync` |

技能、模板、团队、配置、统计与实时通道中已经符合资源语法的 CRUD/子资源路径保留。技能包使用 `/skill-packs/agent-mesh`；团队成员和节点归属为团队子资源。`/api/healthz` 保留。

## 变更与兼容

新功能先扩展已有资源/响应，再判断是否需要新资源或动作。真实调用必须依照 OpenAPI 中可见的规范路径；历史控制面路径作为不出现在 OpenAPI、响应携带 `Deprecation: true` 的临时兼容入口。服务端、Web UI、技能包及 `docs/` 中的示例使用规范路径；删兼容入口前须先确认旧探针和外部客户端升级。`/api/edge/*` 的更改按 [edge-reporting.md](edge-reporting.md) 和探针独立发版流程处理。

接口变更遵守 [docs/README.md](../README.md) 的文档同步规则；OpenAPI 中的公开路径需有自动化断言，避免继续增加未登记的特殊 URL。
