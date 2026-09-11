from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, Response

from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _skills_dir(config: OrchestratorConfig) -> Path:
    return Path(config.db_path).parent / "skills"


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse the YAML frontmatter block of a SKILL.md (only name/description)."""
    stripped = text.lstrip("\ufeff").strip()
    if not stripped.startswith("---"):
        return {}
    end = stripped.find("\n---", 3)
    if end == -1:
        return {}
    block = stripped[3:end]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip("\"'")
            fields[key] = value
    return fields


def _extract_skill_zip(data: bytes) -> tuple[str, str]:
    """Extract ``(name, description)`` from a skill zip.

    Requires a ``SKILL.md`` somewhere in the archive with valid frontmatter
    (``name`` matching opencode's skill-name rule and a non-empty ``description``).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="uploaded file is not a valid zip")

    candidates = [n for n in zf.namelist() if n.endswith("SKILL.md")]
    if not candidates:
        raise HTTPException(status_code=400, detail="zip does not contain a SKILL.md")
    candidates.sort(key=len)
    text = zf.read(candidates[0]).decode("utf-8", errors="replace")

    front = _parse_frontmatter(text)
    name = (front.get("name") or "").strip()
    description = (front.get("description") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="SKILL.md frontmatter is missing 'name'")
    if not _SKILL_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "skill name must match ^[a-z0-9]+(-[a-z0-9]+)*$ "
                f"(got '{name}')"
            ),
        )
    if not description:
        raise HTTPException(status_code=400, detail="SKILL.md frontmatter is missing 'description'")
    return name, description


def mount_skill_routes(
    router: APIRouter,
    store: TaskStore,
    config: OrchestratorConfig,
    require_user_token,
    require_any_token,
) -> None:

    @router.get("/skills")
    async def list_skills(
        _auth: None = Depends(require_any_token),
    ) -> dict[str, Any]:
        """Skill summaries only (name/description/version/enabled), never content."""
        skills = await store.store.list_skills()
        return {
            "skills": [
                {
                    "name": s["name"],
                    "description": s["description"],
                    "version": s["version"],
                    "enabled": s["enabled"],
                }
                for s in skills
            ]
        }

    @router.get("/skills/{name}")
    async def get_skill(
        name: str,
        _auth: None = Depends(require_any_token),
    ) -> dict[str, Any]:
        skill = await store.store.get_skill(name)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        return {
            "skill": {
                "name": skill["name"],
                "description": skill["description"],
                "version": skill["version"],
                "enabled": skill["enabled"],
            }
        }

    @router.post("/skills")
    async def upload_skill(
        request: Request,
        file: UploadFile = File(...),
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Upload a skill as a zip. Server extracts+validates SKILL.md metadata.

        Re-uploading an existing skill name bumps its version (edges detect the
        change and can re-fetch on demand).
        """
        data = await file.read()
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="skill zip too large (max 10MB)")
        name, description = _extract_skill_zip(data)

        skills_dir = _skills_dir(config)
        existing = await store.store.get_skill(name)
        version = (existing["version"] if existing else 0) + 1

        skills_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skills_dir / name / f"{name}.zip"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_bytes(data)

        await store.store.upsert_skill(
            name=name,
            description=description,
            version=version,
            enabled=True,
            filename=skill_file.name,
        )
        logger.info(
            "uploaded skill %s v%d by %s", name, version, user["username"]
        )
        return {
            "skill": {
                "name": name,
                "description": description,
                "version": version,
                "enabled": True,
            }
        }

    @router.patch("/skills/{name}")
    async def patch_skill(
        name: str,
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Enable/disable a skill (edges only browse enabled summaries for download)."""
        skill = await store.store.get_skill(name)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        body = await request.json()
        enabled = body.get("enabled")
        if isinstance(enabled, bool):
            await store.store.update_skill(name, enabled=enabled)
            skill["enabled"] = enabled
        logger.info("updated skill %s by %s", name, user["username"])
        return {
            "skill": {
                "name": skill["name"],
                "description": skill["description"],
                "version": skill["version"],
                "enabled": bool(skill["enabled"]),
            }
        }

    @router.delete("/skills/{name}")
    async def delete_skill(
        name: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        skill = await store.store.get_skill(name)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        deleted = await store.store.delete_skill(name)
        if deleted:
            import shutil

            shutil.rmtree(_skills_dir(config) / name, ignore_errors=True)
        logger.info("deleted skill %s by %s", name, user["username"])
        return {"deleted": deleted}

    @router.get("/skills/{name}/download")
    async def download_skill(
        name: str,
        _auth: None = Depends(require_any_token),
    ) -> FileResponse:
        """Download the full skill zip. Auth: user token or the edge's own token.

        Only the summary endpoints (GET /api/skills) are exposed for browsing;
        the full content is served exclusively here, on demand.
        """
        skill = await store.store.get_skill(name)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        skill_file = _skills_dir(config) / name / f"{name}.zip"
        if not skill_file.exists():
            raise HTTPException(status_code=404, detail="skill file not found")
        return FileResponse(skill_file, filename=f"{name}.zip", media_type="application/zip")

    @router.get("/skill-doc/agent-mesh")
    async def download_agent_mesh_skill(
        request: Request,
        _user: dict[str, Any] = Depends(require_user_token),
    ) -> Response:
        """Download a personalized agent-mesh SKILL.md for the main agent.

        Reads the bundled ``skills/agent-mesh/SKILL.md`` and personalizes it for
        the caller: the configured public URL (``public_url`` setting, the one
        shown on the 配置 page) is filled into every example, and the caller's
        own user token is embedded so the examples are directly runnable. When
        the public URL is not configured, a paragraph tells the agent to ask the
        user for it instead of silently shipping placeholders.
        """
        path = Path(__file__).resolve().parents[4] / "skills" / "agent-mesh" / "SKILL.md"
        if not path.exists():
            raise HTTPException(status_code=404, detail="SKILL.md not found")
        text = path.read_text(encoding="utf-8")

        public_url = (await store.store.get_setting("public_url") or "").strip().rstrip("/")
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        if public_url:
            text = text.replace("http://<orchestrator-host>:8000", public_url)
            text = text.replace("http://<host>:8000", public_url)
            if public_url.endswith(":8000"):
                text = text.replace(
                    "http://<orchestrator-host>:8001",
                    f"{public_url[:-5]}:8001",
                )
            base_line = f"`{public_url}/api`"
        else:
            base_line = "`<未配置 — 请向用户询问编排器地址>`"

        if token:
            text = text.replace("<token>", token)

        info = (
            "## 连接信息（已自动填充）\n\n"
            f"- **REST Base URL**: {base_line}\n"
            f"- **用户 token**: `{token or '<未获取 — 请向用户索取>'}`\n"
            "本页示例中的地址与 token 已填入真实值，可直接复制执行；"
            "token 即本次调用使用的用户凭据，请注意保管，不要外泄/提交到仓库。\n\n"
        )
        text = text.replace("\n## 何时使用\n", f"\n{info}## 何时使用\n", 1)

        return Response(
            content=text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="agent-mesh-SKILL.md"'},
        )
