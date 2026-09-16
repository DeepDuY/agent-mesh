# 文件：任务输入与产物的搬运

解决「主 Agent 所在机器」与「边缘节点工作目录」之间的文件搬运：**把本地文件送过去给任务用**，以及**把任务产生的文件取回来**。

## 何时用 / 不用

- **要用**：任务需要读取/处理某个本地文件 → 走「上传 + `attachments`」。
- **要拿回**：任务产出了文件、用户想看/保存 → 走「产物下载」。
- **不用**：只是一小段文本 → 直接写进 `instruction`。command 模式产生的文件已自动作为 task 产物收集，无需手动上传到文件库。

## 上传本地文件给任务用

```bash
set -a; . ./.env; set +a
# 1) 上传（可多文件；返回 file_id + md5，同内容自动去重）
curl -s -X POST "$AGENT_MESH_BASE_URL/files" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  -F "files=@./data.csv" -F "files=@./config.yaml"
# => {"files":[{"file_id":"f-xxxx","filename":"data.csv","size":123,
#              "content_type":"text/csv","md5":"...","download_url":"/api/files/f-xxxx"}]}

# 2) 派发时引用 file_id
curl -s -X POST "$AGENT_MESH_BASE_URL/tasks/dispatch" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"llm","instruction":"分析工作目录下的 data.csv，输出 summary.md",
      "attachments":["f-xxxx","f-yyyy"]}'
```

- 附件以**原文件名**写入任务工作目录；探针下载后**按 md5 校验**，不一致则任务失败（`summary: attachment download failed`）。
- `mode=command` 也可用 `attachments`：文件先落到工作目录，命令直接引用文件名即可。

## 取回任务产物

```bash
# 任务详情里有 result.artifacts（artifact_id / filename / size）
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/tasks/t-xxxxx"
# 下载单个产物
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" \
  "$AGENT_MESH_BASE_URL/artifacts/t-xxxxx/a-1" -o summary.md
```

- **command 模式**：工作目录下新增文件自动进产物。
- **llm 模式**：只收集 LLM 在最终 JSON 里声明的文件；大文件/多文件要求打包成 `.tar.gz` / `.zip`。
- 产物路径带鉴权，按任务归属过滤；无权访问返回 404。

## 文件库管理

```bash
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/files?search=csv"   # 列表/搜索
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/files/f-xxxx" -o f  # 下载
curl -s -X DELETE -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/files/f-xxxx"
curl -s -X POST -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"file_ids":["f-xxxx","f-yyyy"]}' "$AGENT_MESH_BASE_URL/files/batch-download" -o files.zip
```

- 文件库按内容去重。
- **删除不做引用校验**：被删的文件如果后续任务还在用它，任务执行时会因下载失败而失败。删除前和用户确认。
