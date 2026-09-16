from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.limits import MB, get_int_setting
from agent_mesh.orchestrator.task_store import TaskStore

logger = logging.getLogger(__name__)


class _BatchFilesPayload(BaseModel):
    file_ids: list[str] = Field(default_factory=list)


def _files_dir(config: OrchestratorConfig) -> Path:
    return Path(config.db_path).parent / "files"


def _new_file_id() -> str:
    return f"f-{uuid.uuid4().hex[:8]}"


def _file_to_ref(row: dict[str, Any]) -> dict[str, Any]:
    """Authoritative public shape of a file-library entry."""
    return {
        "file_id": row["file_id"],
        "filename": row["filename"],
        "size": row["size"],
        "content_type": row.get("content_type") or "application/octet-stream",
        "md5": row["md5"],
        "download_url": f"/api/files/{row['file_id']}",
    }


async def _delete_file(
    store: TaskStore,
    config: OrchestratorConfig,
    file_id: str,
    user: dict[str, Any] | None = None,
) -> bool:
    """Delete a file's DB row + on-disk blob. Returns False if not found.

    A non-admin may only delete files they own.
    """
    row = await store.store.get_file(file_id)
    if row is None:
        return False
    if user is not None and not store.is_admin(user):
        owner_ids = {user.get("user_id"), user.get("username")}
        if row.get("created_by") not in owner_ids:
            return False
    deleted = await store.store.delete_file(file_id)
    if deleted:
        path = _files_dir(config) / f"{file_id}_{row['filename']}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove file blob %s", path)
        logger.info("deleted file %s by request", file_id)
    return deleted


def mount_file_routes(
    router: APIRouter,
    store: TaskStore,
    config: OrchestratorConfig,
    require_user_token,
    require_any_token,
) -> None:

    @router.post("/files")
    async def upload_files(
        files: list[UploadFile] = File(...),
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Upload files into the library (multipart `files`, multiple allowed).

        Deduplicates by (md5 + filename): re-uploading identical content returns
        the existing ``file_id``. Returns the authoritative FileRefs, including
        ``md5`` and ``download_url``, for the dispatcher to attach to a task.
        """
        if not files:
            raise HTTPException(status_code=400, detail="no files provided")
        files_dir = _files_dir(config)
        files_dir.mkdir(parents=True, exist_ok=True)
        max_size = await get_int_setting(store, "file_max_size_mb") * MB
        refs: list[dict[str, Any]] = []
        for upload in files:
            content = await upload.read()
            if len(content) > max_size:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"file {upload.filename} exceeds max size "
                        f"{max_size // MB}MB"
                    ),
                )
            md5 = hashlib.md5(content).hexdigest()
            safe_name = Path(upload.filename or "unnamed").name
            owner_id = user.get("user_id") or user.get("username")
            dedup_owner = None if store.is_admin(user) else owner_id
            existing = await store.store.find_file_by_md5(md5, safe_name, dedup_owner)
            if existing:
                refs.append(_file_to_ref(existing))
                continue
            file_id = _new_file_id()
            dest = files_dir / f"{file_id}_{safe_name}"
            dest.write_bytes(content)
            content_type, _ = mimetypes.guess_type(safe_name)
            row = {
                "file_id": file_id,
                "filename": safe_name,
                "size": len(content),
                "content_type": content_type or "application/octet-stream",
                "md5": md5,
            }
            await store.store.create_file(
                file_id=file_id,
                filename=safe_name,
                size=len(content),
                content_type=row["content_type"],
                md5=md5,
                created_by=owner_id,
            )
            logger.info(
                "uploaded file %s (%s, %d bytes) by %s",
                file_id, safe_name, len(content), user["username"],
            )
            refs.append(_file_to_ref(row))
        return {"files": refs}

    @router.get("/files")
    async def list_files(
        search: str | None = None,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        owner = None if store.is_admin(user) else (user.get("user_id") or user.get("username"))
        rows = await store.store.list_files(search=search or None, owner=owner)
        for r in rows:
            r.setdefault("download_url", f"/api/files/{r['file_id']}")
        return {"files": rows}

    @router.get("/files/{file_id}")
    async def download_file(
        file_id: str,
        auth: dict[str, Any] = Depends(require_any_token),
    ):
        """Download a library file. Auth: user token or the edge's own token.

        The edge fetches attached files with its own token before executing a
        task and verifies integrity against ``X-File-Md5``. User callers may
        only download files they own (admin: all).
        """
        row = await store.store.get_file(file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="file not found")
        if auth.get("auth") == "user" and not store.is_admin(auth):
            owner_ids = {auth.get("user_id"), auth.get("username")}
            if row.get("created_by") not in owner_ids:
                raise HTTPException(status_code=404, detail="file not found")
        path = _files_dir(config) / f"{file_id}_{row['filename']}"
        if not path.exists():
            raise HTTPException(status_code=404, detail="file not found")
        content_type, _ = mimetypes.guess_type(row["filename"])
        return FileResponse(
            path,
            filename=row["filename"],
            media_type=content_type or "application/octet-stream",
            headers={"X-File-Md5": row["md5"]},
        )

    @router.delete("/files/{file_id}")
    async def delete_file(
        file_id: str,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Delete a library file. References are not checked; a task that later
        references a deleted file will fail at attachment-download time."""
        deleted = await _delete_file(store, config, file_id, user)
        if not deleted:
            raise HTTPException(status_code=404, detail="file not found")
        return {"deleted": deleted}

    @router.post("/files/batch-delete")
    async def batch_delete_files(
        payload: _BatchFilesPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> dict[str, Any]:
        """Delete multiple library files (DB rows + on-disk blobs)."""
        deleted = 0
        for file_id in payload.file_ids:
            if await _delete_file(store, config, file_id, user):
                deleted += 1
        logger.info(
            "batch-deleted %d files (requested %d) by %s",
            deleted, len(payload.file_ids), user["username"],
        )
        return {"deleted": deleted, "total": len(payload.file_ids)}

    @router.post("/files/batch-download")
    async def batch_download_files(
        payload: _BatchFilesPayload,
        user: dict[str, Any] = Depends(require_user_token),
    ) -> Response:
        """Download multiple library files as a single ZIP archive."""
        if not payload.file_ids:
            raise HTTPException(status_code=400, detail="no files selected")
        rows = await store.store.get_files_by_ids(payload.file_ids)
        if not store.is_admin(user):
            owner_ids = {user.get("user_id"), user.get("username")}
            rows = [r for r in rows if r.get("created_by") in owner_ids]
        files_dir = _files_dir(config)
        buf = io.BytesIO()
        used: set[str] = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for row in rows:
                path = files_dir / f"{row['file_id']}_{row['filename']}"
                if not path.exists():
                    continue
                name = row["filename"]
                if name in used:
                    name = f"{row['file_id']}_{name}"
                used.add(name)
                zf.write(path, name)
        buf.seek(0)
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="files.zip"'},
        )
