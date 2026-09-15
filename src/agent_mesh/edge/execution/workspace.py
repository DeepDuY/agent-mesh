from __future__ import annotations

from pathlib import Path


def _cleanup_config(config_path: Path) -> None:
    try:
        if config_path and config_path.exists():
            config_path.unlink()
    except Exception:
        pass


def _snapshot_files(workdir: Path) -> set[str]:
    snapshot: set[str] = set()
    if not workdir.exists():
        return snapshot
    for p in workdir.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(workdir).parts):
            snapshot.add(str(p.relative_to(workdir)))
    return snapshot


def _collect_artifacts(workdir: Path, pre_snapshot: set[str]) -> list[str]:
    artifacts: list[str] = []
    if not workdir.exists():
        return artifacts
    for p in workdir.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(workdir).parts):
            rel = str(p.relative_to(workdir))
            if rel not in pre_snapshot:
                artifacts.append(rel)
    return artifacts
