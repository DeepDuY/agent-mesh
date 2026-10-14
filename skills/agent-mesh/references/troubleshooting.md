# 故障处理

| 现象 | 检查与处理 |
|---|---|
| `401` | 用户 token 失效；按 [认证](auth.md) 更新 `.env`。 |
| `403` / `denied` | 阅读响应 `detail` 或任务 `result.summary`；命令权限或 AI 审核拦截时停止重复派发。 |
| `pending_review` | 任务未入队；等待 Web 审核通过、拒绝或到期。 |
| 长时间 `queued` | 查询节点 `online`、队列并发占用、`depends_on` 的状态；离线或依赖未完成会阻止执行。 |
| `no LLM model configured` | 目标节点缺少可用模型；请求管理员配置后再派发。 |
| `attachment download failed` | 文件不存在或 md5 不符；检查文件库 ID，必要时重新上传并重新派发。 |
| `404` | 核对资源 ID 及当前用户的节点/任务权限；产物也可能已清理。 |

任务进度使用 `GET /tasks/{id}`，日志使用 `GET /tasks/{id}/logs?after_id=...`，事件使用 `GET /tasks/{id}/events`。管理员配置与授权在 Web 管理平台处理。
