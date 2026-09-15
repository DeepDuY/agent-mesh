from __future__ import annotations

import mimetypes
import re
import threading
import tempfile
import uuid
from pathlib import Path

from agent_mesh.shared.schemas import ArtifactRef

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_task_id(task_id: str) -> str | None:
    """Return a path-safe task id, or None when it could escape base_dir.

    Task ids are generated as ``t-<hex>``; anything containing path separators
    or ``.``/``..`` is rejected so a crafted URL cannot traverse the artifact
    directory.
    """
    if not task_id or not _TASK_ID_RE.match(task_id) or task_id in (".", ".."):
        return None
    return task_id


class ArtifactStore:
    def __init__(
        self,
        base_dir: str,
        max_size_mb: int = 50,
        max_total_mb: int = 200,
    ):
        if base_dir == ":memory:":
            # Tests use ":memory:" as a stand-in for "no persistent storage".
            # Never resolve it into a real directory in the CWD (that would
            # create a stray `:memory:/` folder); use a throwaway temp dir.
            self.base_dir = Path(tempfile.mkdtemp(prefix="artifact-mem-"))
        else:
            self.base_dir = Path(base_dir).expanduser().resolve()
            self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_size = max_size_mb * 1024 * 1024
        self.max_total = max_total_mb * 1024 * 1024
        self._lock = threading.Lock()

    def _task_dir(self, task_id: str) -> Path:
        safe = _safe_task_id(task_id)
        if safe is None:
            raise ValueError(f"invalid task id: {task_id!r}")
        d = self.base_dir / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _total_size(self) -> int:
        total = 0
        for p in self.base_dir.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return total

    def save(
        self, task_id: str, filename: str, content: bytes
    ) -> ArtifactRef:
        # Serialize the size checks and the file write so concurrent uploads
        # cannot race past the total-storage quota (TOCTOU).
        with self._lock:
            if len(content) > self.max_size:
                raise ValueError(
                    f"artifact {filename} exceeds max size {self.max_size} bytes"
                )
            if self._total_size() + len(content) > self.max_total:
                raise ValueError("artifact upload exceeds total storage quota")

            artifact_id = f"a-{uuid.uuid4().hex[:8]}"
            # Sanitize filename.
            safe_name = Path(filename).name
            dest = self._task_dir(task_id) / f"{artifact_id}_{safe_name}"
            with open(dest, "wb") as f:
                f.write(content)

        content_type, _ = mimetypes.guess_type(safe_name)
        content_type = content_type or "application/octet-stream"
        download_url = f"/api/artifacts/{task_id}/{artifact_id}"
        return ArtifactRef(
            filename=safe_name,
            size=len(content),
            content_type=content_type,
            download_url=download_url,
            artifact_id=artifact_id,
        )

    def list(self, task_id: str) -> list[ArtifactRef]:
        safe = _safe_task_id(task_id)
        if safe is None:
            return []
        task_dir = self.base_dir / safe
        if not task_dir.exists():
            return []
        refs: list[ArtifactRef] = []
        for p in sorted(task_dir.iterdir()):
            if p.is_file():
                # Parse artifact_id from filename: {artifact_id}_{filename}
                name = p.name
                if "_" in name:
                    artifact_id, _, filename = name.partition("_")
                else:
                    artifact_id = name
                    filename = name
                content_type, _ = mimetypes.guess_type(filename)
                refs.append(
                    ArtifactRef(
                        filename=filename,
                        size=p.stat().st_size,
                        content_type=content_type or "application/octet-stream",
                        download_url=f"/api/artifacts/{task_id}/{artifact_id}",
                        artifact_id=artifact_id,
                    )
                )
        return refs

    def resolve(self, task_id: str, artifact_id: str) -> tuple[Path, str] | None:
        """Return (path, original_filename) or None if not found."""
        safe = _safe_task_id(task_id)
        if safe is None:
            return None
        task_dir = self.base_dir / safe
        if not task_dir.exists():
            return None
        for p in task_dir.iterdir():
            if p.is_file() and p.name.startswith(f"{artifact_id}_"):
                filename = p.name.partition("_")[2]
                return p, filename
        return None
