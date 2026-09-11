#!/usr/bin/env python3
"""Build self-contained agent bootstrap packages for Linux/macOS.

Run on the orchestrator machine (which has internet access) to produce:
    data/bootstrap/agent-mesh-agent-{linux|darwin}-{x64|arm64}.tar.gz
    data/bootstrap/install.sh

The package contains:
    bin/agent-mesh-edge   PyInstaller-built edge agent binary
    bin/opencode          opencode CLI binary (copied from ~/.opencode/bin/opencode)
    install.sh            one-command installer
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
BOOTSTRAP_DIR = PROJECT_ROOT / "data" / "bootstrap"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def _copy_opencode(dest_bin: Path) -> bool:
    candidates = [
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".opencode" / "bin" / "opencode.exe",
    ]
    for c in candidates:
        if c.exists():
            dest_bin.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c, dest_bin)
            return True
    return False


def _detect_target() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        os_name = "darwin"
    else:
        os_name = "linux"
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = "x64"
    return os_name, arch


def _build_binary() -> Path:
    """Build the PyInstaller binary for the edge agent.

    Uses the currently-running interpreter's PyInstaller (``sys.executable
    -m PyInstaller``) instead of ``uv run pyinstaller``: the latter re-resolves
    the environment and can attempt a source build of ``asyncpg`` (no prebuilt
    wheel) that fails under conda-injected compiler flags. Requires pyinstaller
    installed in the running environment (dev extra: ``uv pip install pyinstaller``).
    """
    spec = PROJECT_ROOT / "scripts" / "agent-mesh-edge.spec"
    _run([
        sys.executable, "-m", "PyInstaller",
        str(spec),
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--noconfirm",
    ])

    exe = DIST_DIR / "agent-mesh-edge"
    if not exe.exists():
        raise RuntimeError("PyInstaller did not produce dist/agent-mesh-edge")
    return exe


def build(os_name: str | None = None, arch: str | None = None) -> Path:
    detected_os, detected_arch = _detect_target()
    os_name = os_name or detected_os
    arch = arch or detected_arch

    BOOTSTRAP_DIR.mkdir(parents=True, exist_ok=True)

    exe = _build_binary()

    with tempfile.TemporaryDirectory(prefix="agent-bootstrap-") as td:
        work = Path(td)
        pkg = work / f"agent-mesh-agent-{os_name}-{arch}"
        pkg.mkdir()
        bin_dir = pkg / "bin"
        bin_dir.mkdir()

        # Copy PyInstaller binary.
        shutil.copy2(exe, bin_dir / "agent-mesh-edge")

        # Copy opencode binary.
        opencode_dest = bin_dir / "opencode"
        if not _copy_opencode(opencode_dest):
            print("WARNING: opencode binary not found; agent will need to install it manually")

        # Copy install.sh from data/bootstrap (canonical version)
        install_src = PROJECT_ROOT / "data" / "bootstrap" / "install.sh"
        if install_src.exists():
            shutil.copy2(install_src, pkg / "install.sh")
        else:
            print("WARNING: data/bootstrap/install.sh not found; package will lack installer")

        # Write the version manifest into the package and the bootstrap dir.
        # The orchestrator reads data/bootstrap/VERSION to decide whether an edge
        # agent's reported version is stale (see orchestrator/api/edge.py).
        from agent_mesh.shared.constants import VERSION

        version_path = pkg / "VERSION"
        version_path.write_text(VERSION, encoding="utf-8")
        (BOOTSTRAP_DIR / "VERSION").write_text(VERSION, encoding="utf-8")
        print(f"wrote VERSION={VERSION} to {BOOTSTRAP_DIR / 'VERSION'}")

        # Build tar.gz.
        tar_name = f"agent-mesh-agent-{os_name}-{arch}.tar.gz"
        tar_path = BOOTSTRAP_DIR / tar_name
        _run(["tar", "-czf", str(tar_path), "-C", str(work), pkg.name])
        print(f"created {tar_path}")

        # Also keep a copy of install.sh at bootstrap root for direct curl.
        shutil.copy2(pkg / "install.sh", BOOTSTRAP_DIR / "install.sh")
        return tar_path


if __name__ == "__main__":
    build()
