# 文件与任务产物

文件库用于上传任务输入，任务产物通过任务 ID 下载。当前目录为技能包根目录，命令保留完整认证头。

## 上传并引用文件

```bash
set -a; . ./.env; set +a
curl -sS -X POST "$AGENT_MESH_BASE_URL/files" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -F "files=@./data.csv"
# => {"files":[{"file_id":"f-...","md5":"...","download_url":"/api/files/f-...",...}]}
curl -sS -X POST "$AGENT_MESH_BASE_URL/tasks" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"llm","instruction":"分析 data.csv 并生成 summary.md","attachments":["f-xxxxx"]}'
```

上传响应中的 `file_id` 用于 `attachments`。探针按原文件名下载到工作目录并核验 md5；文件缺失或校验失败时任务执行失败。`command` 与 `llm` 模式均支持附件。

## 下载产物

从 `GET /tasks/{id}` 的 `task.result.artifacts[]` 读取 `artifact_id`、`filename` 与 `download_url`；使用任务归属权限下载：

```bash
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/tasks/t-xxxxx/artifacts/a-xxxxx" -o summary.md
```

`command` 模式收集工作目录下新增文件；`llm` 模式根据执行结果中声明的产物收集。产物不存在或无权限时返回 404。

## 文件库管理

```bash
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/files?search=csv"
curl -sS -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/files/f-xxxxx" -o data.csv
curl -sS -X POST "$AGENT_MESH_BASE_URL/files:batch_download" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"file_ids":["f-xxxxx"]}' -o files.zip
```

`DELETE /files/{id}` 删除文件库条目和文件，但不检查任务引用；在后续任务需要该文件时会导致下载失败。删除前确认任务依赖。
