from __future__ import annotations


def _wrap_llm_instruction(instruction: str, system_prompt: str = "") -> str:
    role_block = ""
    if system_prompt.strip():
        role_block = (
            "## 角色与上下文\n"
            f"{system_prompt.strip()}\n\n"
        )
    return (
        role_block
        + "You are an agent task executor. Follow the user's instruction, use tools as needed, "
        "and produce the final answer in the exact JSON format below. Do not output anything "
        "after the JSON block.\n\n"
        "```json\n"
        "{\n"
        '  "summary": "one-line summary of the result",\n'
        '  "answer": "the full answer to return to the caller",\n'
        '  "artifacts": [\n'
        '    {"path": "relative/path/under/workdir", "compressed": true}\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "Rules for artifacts:\n"
        "- Only list files you actually created in the workdir.\n"
        "- Paths must be relative to the workdir.\n"
        "- Large files (>1 MB) or multiple related files must be compressed into a .tar.gz or .zip.\n"
        "- If no artifacts, use an empty list [].\n\n"
        f"User instruction: {instruction}\n\n"
        "## 任务附件\n"
        "工作目录下可能已放入任务附件文件（派发时附加）。执行前先用 ls 查看工作目录，"
        "若存在附件文件则按需读取使用；若指令与附件无关可直接忽略本段。\n\n"
        "## 技能库（按需使用）\n"
        "本环境提供技能库。若当前任务需要专业技能，可自行通过以下方式查看并按需下载使用；"
        "没有合适技能时直接忽略本段，无需特意访问：\n"
        "- 查看可用技能摘要：`curl -s -H \"Authorization: Bearer ${EDGE_TOKEN:-}\" \"${ORCHESTRATOR_URL:-}/api/skills\"`\n"
        "- 若从摘要中发现合适的技能，下载其压缩包并解压使用（把 <skill-name> 换成实际技能名）：\n"
        "  `curl -s -H \"Authorization: Bearer ${EDGE_TOKEN:-}\" -o /tmp/skill.zip \"${ORCHESTRATOR_URL:-}/api/skills/<skill-name>/download\" "
        "&& mkdir -p .opencode/skills/<skill-name> && unzip -o /tmp/skill.zip -d .opencode/skills/<skill-name> "
        "&& cat .opencode/skills/<skill-name>/SKILL.md`\n"
        "- 阅读 SKILL.md 后按其说明执行；下载的文件若只是参考说明，不必列为产物。\n\n"
        "Now produce the final JSON:"
    )
