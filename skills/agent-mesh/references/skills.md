# 技能库（给远程 LLM 用的专业知识）

技能是打包好的 `SKILL.md` 指南，供**边缘节点的 LLM 在任务中自主取用**——相当于给远程的 AI 临时装上某个专业流程/规范。

## 何时用

- 希望远程 `llm` 任务按某个专业规范工作时（例如「按我们团队的部署规范来做」）。
- 通常**只需在 `instruction` 里提示**要让 LLM 参考哪个技能，边沿会自行浏览并下载；不需要你手动下载再上传。

## 何时不用

- 任务简单、无需额外知识时。
- 用 `command` 模式（不经过 LLM）时技能没有意义。

## 浏览与下载

```bash
set -a; . ./.env; set +a
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/skills"          # 摘要（name/description/version/enabled）
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/skills/<name>"   # 单个摘要
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/skills/<name>/download" -o skill.zip  # 完整内容
```

- 摘要接口（`GET /skills`）是给浏览用的，只返回元信息，**不含内容**；完整内容只在 `.../download` 按需拉取。

## 在任务中使用

```bash
curl -s -X POST "$AGENT_MESH_BASE_URL/tasks/dispatch" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"llm","instruction":"按 <技能名> 的规范完成：<具体任务>","skills":["<技能名>"]}'
```

> `skills` 参数是提示（预留）；边沿可自主浏览已启用技能。重点是 `instruction` 里点明要用哪个技能、达成什么。

## 管理（一般由管理员在 Web「技能」页操作）

`POST /skills`（上传 zip，需含带 `name`/`description` frontmatter 的 `SKILL.md`；重传同名 version+1）、`PATCH /skills/{name}`（启用/停用）、`DELETE /skills/{name}`。

## 本技能自身的分发

本 agent-mesh 技能包由用户从 Web 控制台「个人中心」下载（`GET /api/skill-pack/agent-mesh`，返回 zip，内含个人 `.env`）。这里不再明文内嵌 token。
