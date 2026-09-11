#!/usr/bin/env python3
"""Rewrite venv symlinks so they point to the bundled Python runtime.

Usage: relocate_venv.py <venv_dir> <python_runtime_dir>
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def relocate(venv_dir: Path, runtime_dir: Path) -> None:
    for root, _dirs, files in os.walk(venv_dir):
        for name in files:
            path = Path(root) / name
            if not path.is_symlink():
                continue
            target = path.readlink()
            if not target.is_absolute():
                continue
            # If the symlink points inside the runtime dir, rewrite it relative.
            try:
                resolved = target.resolve()
                runtime_resolved = runtime_dir.resolve()
                try:
                    resolved.relative_to(runtime_resolved)
                except ValueError:
                    continue
                rel = os.path.relpath(resolved, start=path.parent)
                path.unlink()
                os.symlink(rel, path)
                print(f"rewrote {path} -> {rel}")
            except Exception as e:
                print(f"WARNING: could not rewrite symlink {path}: {e}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <venv_dir> <python_runtime_dir>", file=sys.stderr)
        sys.exit(1)
    relocate(Path(sys.argv[1]), Path(sys.argv[2]))
