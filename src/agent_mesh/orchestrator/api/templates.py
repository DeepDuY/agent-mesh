from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")


class _TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    system_prompt: str | None = None
    llm_model: str | None = None
    allowed_tools: list[str] | None = None
    data: dict[str, Any] | None = None


class _TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    system_prompt: str | None = None
    llm_model: str | None = None
    allowed_tools: list[str] | None = None
    data: dict[str, Any] | None = None


def _public(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": template["id"],
        "name": template["name"],
        "description": template.get("description"),
        "system_prompt": template.get("system_prompt"),
        "llm_model": template.get("llm_model"),
        "allowed_tools": template.get("allowed_tools"),
        "data": template.get("data"),
        "created_at": _iso(template.get("created_at")),
        "updated_at": _iso(template.get("updated_at")),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def mount_template_routes(
    router: APIRouter,
    store: TaskStore,
    require_user_token,
) -> None:

    @router.get("/templates")
    async def list_templates(
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        templates = await store.store.list_templates()
        return {"templates": [_public(t) for t in templates]}

    @router.post("/templates")
    async def create_template(
        payload: _TemplateCreate,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        name = payload.name.strip()
        if not _NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail="name may only contain letters, digits, '.', '_' and '-'",
            )
        if await store.store.get_template_by_name(name) is not None:
            raise HTTPException(status_code=409, detail="template name already exists")
        template_id = await store.store.create_template(
            name=name,
            description=payload.description,
            system_prompt=payload.system_prompt,
            llm_model=payload.llm_model,
            allowed_tools=payload.allowed_tools,
            data=payload.data,
        )
        await store.bump_config_version()
        template = await store.store.get_template(template_id)
        logger.info("created template %s (%s) by %s", template_id, name, user["username"])
        return {"template": _public(template)}

    @router.get("/templates/{template_id}")
    async def get_template(
        template_id: int,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        template = await store.store.get_template(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="template not found")
        return {"template": _public(template)}

    @router.patch("/templates/{template_id}")
    async def patch_template(
        template_id: int,
        payload: _TemplatePatch,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        template = await store.store.get_template(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="template not found")
        fields = payload.model_dump(exclude_unset=True)
        if "name" in fields and fields["name"] is not None:
            name = fields["name"].strip()
            if not _NAME_RE.match(name):
                raise HTTPException(
                    status_code=400,
                    detail="name may only contain letters, digits, '.', '_' and '-'",
                )
            existing = await store.store.get_template_by_name(name)
            if existing is not None and existing["id"] != template_id:
                raise HTTPException(status_code=409, detail="template name already exists")
            fields["name"] = name
        await store.store.update_template(template_id, **fields)
        # Template changes affect every bound node -> push via config sync.
        await store.bump_config_version()
        template = await store.store.get_template(template_id)
        logger.info("updated template %s by %s", template_id, user["username"])
        return {"template": _public(template)}

    @router.delete("/templates/{template_id}")
    async def delete_template(
        template_id: int,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        deleted = await store.store.delete_template(template_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="template not found")
        await store.bump_config_version()
        logger.info("deleted template %s by %s", template_id, user["username"])
        return {"deleted": True}
