# 远程技能库

本技能包指导主 Agent 调用编排器；远程技能库中的技能供节点执行 `llm` 任务时使用，`command` 任务不加载技能。

## 查询与指定

```bash
set -a; . ./.env; set +a
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/skills"
curl -sS -X POST "$AGENT_MESH_BASE_URL/tasks" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"llm","instruction":"按 deploy-helper 技能执行指定部署任务","skills":["deploy-helper"]}'
```

`GET /skills` 仅返回名称、描述、版本及启用状态。派发时 `skills` 应指定存在且 `enabled=true` 的名称；服务端校验后由探针按需下载并注入内容。仅在 `instruction` 提及技能而未设置 `skills` 时，不保证探针会加载该技能。

需要读取完整内容时，使用 `GET /skills/{name}/download` 下载 zip；查询单项摘要使用 `GET /skills/{name}`。技能上传、编辑和停用由管理端处理。
