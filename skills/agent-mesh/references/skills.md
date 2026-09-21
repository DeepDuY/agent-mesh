# 技能库（给远程 LLM 用的专业知识）

技能是打包好的 `SKILL.md` 指南，供**边缘节点的 LLM 在 `llm` 任务中按需使用**——相当于给远程的 AI 临时装上某个专业流程/规范。

> 注意：这里说的是**编排器上的远程技能库**（管理员上传、动态增删），不是**你正在使用的 agent-mesh 技能包**。

## 何时用

- 希望远程 `llm` 任务按某个专业规范工作时（例如「按我们团队的部署规范来做」）。
- 用 `command` 模式（不经过 LLM）时技能没有意义。

## 推荐做法

1. 先 `GET /skills` 看可用技能（关注 `enabled=true` 的 `name` 与 `description`）。
2. 在 `llm` 任务的 `instruction` 里**点名技能**，例如：`"按 deploy-helper 技能完成：<具体任务>"`。
3. 可选：把技能名同时放进 `skills` 字段（`"skills": ["deploy-helper"]`）。服务端会校验该技能存在且启用；边沿执行时会拉取并注入其内容（节点需为支持该特性的版本，否则退化为节点 LLM 按提示自行获取）。
4. **不要**自己下载技能再上传——远程节点会按需获取。

## 浏览与下载

```bash
set -a; . ./.env; set +a
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/skills"          # 摘要（name/description/version/enabled）
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/skills/<name>"   # 单个摘要
curl -s -H "Authorization: Bearer $AGENT_MESH_TOKEN" "$AGENT_MESH_BASE_URL/skills/<name>/download" -o skill.zip  # 完整内容
```

- 摘要接口（`GET /skills`）只返回元信息，**不含内容**；完整内容只在 `.../download` 按需拉取。

## 在任务中使用

```bash
curl -s -X POST "$AGENT_MESH_BASE_URL/tasks/dispatch" \
  -H "Authorization: Bearer $AGENT_MESH_TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":3,"mode":"llm","instruction":"按 deploy-helper 技能完成：<具体任务>","skills":["deploy-helper"]}'
```

- `skills` 字段是技能名列表；**服务端会校验**——名字不存在或已停用时 `POST /tasks/dispatch` 直接返回 `400`。
- 重点是 `instruction` 里点明要用哪个技能、达成什么；`skills` 字段用于强制边沿注入该技能内容。
- 边沿执行 `llm` 任务时，若指定了 `skills` 会拉取对应技能并注入；未指定时提示词中也会带技能库使用指引，由节点 LLM 自主决定是否取用。

## 管理（一般由管理员在 Web「技能」页操作）

`POST /skills`（上传 zip，需含带 `name`/`description` frontmatter 的 `SKILL.md`；重传同名 version+1）、`PATCH /skills/{name}`（启用/停用）、`DELETE /skills/{name}`。

## 本技能自身的分发

本 agent-mesh 技能包由用户从 Web 控制台「个人中心」下载（`GET /api/skill-pack/agent-mesh`，返回 zip，内含个人 `.env`）。这里不再明文内嵌 token。
