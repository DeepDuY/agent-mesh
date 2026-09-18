from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
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


def _find_skill_md(zf: zipfile.ZipFile) -> str | None:
    """The SKILL.md member inside a skill zip (shortest path wins)."""
    candidates = [n for n in zf.namelist() if n.endswith("SKILL.md")]
    if not candidates:
        return None
    candidates.sort(key=len)
    return candidates[0]


def _read_skill_md(skill_file: Path) -> str | None:
    try:
        with zipfile.ZipFile(skill_file) as zf:
            member = _find_skill_md(zf)
            return zf.read(member).decode("utf-8", errors="replace") if member else None
    except (OSError, zipfile.BadZipFile):
        return None


def _write_skill_zip(skill_file: Path, content: str) -> None:
    """Create or update a skill zip's SKILL.md, preserving other members.

    Editing a skill only replaces its SKILL.md; any other files (e.g.
    ``references/``) in the uploaded archive are kept as-is.
    """
    payload = content.encode("utf-8")
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    items: list[tuple[zipfile.ZipInfo, bytes]] = []
    if skill_file.exists():
        try:
            with zipfile.ZipFile(skill_file) as zf:
                items = [(info, zf.read(info.filename)) for info in zf.infolist()]
        except (OSError, zipfile.BadZipFile):
            items = []

    target_member = None
    for info, _ in items:
        if info.filename.endswith("SKILL.md"):
            target_member = info.filename
            break

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        replaced = False
        for info, data in items:
            if info.filename == target_member:
                out.writestr(info, payload)
                replaced = True
            else:
                out.writestr(info, data)
        if not replaced:
            out.writestr("SKILL.md", payload)
    skill_file.write_bytes(buf.getvalue())


def _validate_skill_content(name: str, content: str) -> str:
    """Validate raw SKILL.md text; returns the description."""
    front = _parse_frontmatter(content)
    fm_name = (front.get("name") or "").strip()
    description = (front.get("description") or "").strip()
    if not fm_name:
        raise HTTPException(
            status_code=400, detail="SKILL.md frontmatter is missing 'name'"
        )
    if fm_name != name:
        raise HTTPException(
            status_code=400,
            detail=f"SKILL.md frontmatter name '{fm_name}' must match the skill name '{name}'",
        )
    if not description:
        raise HTTPException(
            status_code=400, detail="SKILL.md frontmatter is missing 'description'"
        )
    return description


_UNCONFIGURED_URL_NOTE = (
    "> ⚠️ 本技能包尚未配置「公开地址」，`.env` 的 `AGENT_MESH_BASE_URL` 为空，"
    "示例中的地址仍是占位符。请先向用户索取编排器地址，填入 `.env` 后再执行示例。\n\n"
)

_SKILL_TITLE = "# agent-mesh 编排器控制"


def _personalize_text(text: str, public_url: str) -> str:
    """Fill the configured public URL into every example placeholder."""
    if not public_url:
        return text
    text = text.replace("http://<orchestrator-host>:8000", public_url)
    text = text.replace("http://<host>:8000", public_url)
    return text


def _inject_unconfigured_note(text: str) -> str:
    """Insert a warning right before the title when no public URL is set."""
    if _SKILL_TITLE in text:
        return text.replace(_SKILL_TITLE, f"{_UNCONFIGURED_URL_NOTE}{_SKILL_TITLE}", 1)
    return f"{_UNCONFIGURED_URL_NOTE}{text}"


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

    @router.get("/skills/{name}/content")
    async def get_skill_content(
        name: str,
        _user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Raw SKILL.md text for the page editor (user token only)."""
        skill = await store.store.get_skill(name)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        skill_file = _skills_dir(config) / name / f"{name}.zip"
        content = _read_skill_md(skill_file) if skill_file.exists() else None
        if content is None:
            raise HTTPException(status_code=404, detail="SKILL.md not found in skill archive")
        return {
            "skill": {
                "name": skill["name"],
                "description": skill["description"],
                "version": skill["version"],
                "enabled": skill["enabled"],
            },
            "content": content,
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

    @router.put("/skills/{name}")
    async def put_skill(
        name: str,
        request: Request,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Create or update a skill from raw SKILL.md text (page editor).

        Creating a new skill needs only a name + a SKILL.md with ``name`` (must
        equal the skill name) and ``description`` frontmatter. Editing an
        existing skill replaces its SKILL.md and bumps its version; other files
        in the archive are preserved.
        """
        if not _SKILL_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail="skill name must match ^[a-z0-9]+(-[a-z0-9]+)*$",
            )
        body = await request.json()
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=400, detail="content is required")
        description = _validate_skill_content(name, content)

        skill_file = _skills_dir(config) / name / f"{name}.zip"
        existing = await store.store.get_skill(name)
        enabled = bool(existing["enabled"]) if existing else True
        version = (existing["version"] if existing else 0) + 1
        _write_skill_zip(skill_file, content)
        await store.store.upsert_skill(
            name=name,
            description=description,
            version=version,
            enabled=enabled,
            filename=skill_file.name,
        )
        logger.info(
            "%s skill %s v%d by %s",
            "updated" if existing else "created", name, version, user["username"],
        )
        return {
            "skill": {
                "name": name,
                "description": description,
                "version": version,
                "enabled": enabled,
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

    @router.get("/skill-pack/agent-mesh")
    async def download_agent_mesh_skill_pack(
        request: Request,
        _user: dict[str, Any] = Depends(require_user_token),
    ) -> Response:
        """Download the bundled agent-mesh skill as a zip, personalized for the caller.

        The returned ``agent-mesh.zip`` contains the SKILL.md index, its
        ``references/`` documents (with the configured public URL filled into
        every example) and a generated ``agent-mesh/.env`` holding the caller's
        own ``AGENT_MESH_BASE_URL`` / ``AGENT_MESH_TOKEN``. The token is never
        written into the Markdown itself.
        """
        pack_dir = Path(__file__).resolve().parents[4] / "skills" / "agent-mesh"
        skill_md = pack_dir / "SKILL.md"
        if not skill_md.exists():
            raise HTTPException(status_code=404, detail="skill pack not found")

        public_url = (await store.store.get_setting("public_url") or "").strip().rstrip("/")
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(pack_dir.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                rel = path.relative_to(pack_dir).as_posix()
                if path.suffix.lower() == ".md":
                    text = _personalize_text(path.read_text(encoding="utf-8"), public_url)
                    if not public_url and rel == "SKILL.md":
                        text = _inject_unconfigured_note(text)
                    zf.writestr(f"agent-mesh/{rel}", text)
                else:
                    zf.writestr(f"agent-mesh/{rel}", path.read_bytes())

            env_lines = [
                "# Auto-generated for your account by agent-mesh. Keep it secret.",
                f"AGENT_MESH_BASE_URL={(public_url + '/api') if public_url else ''}",
                f"AGENT_MESH_TOKEN={token}",
            ]
            zf.writestr("agent-mesh/.env", "\n".join(env_lines) + "\n")

        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="agent-mesh.zip"'},
        )
