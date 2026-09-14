#!/usr/bin/env python3
"""Build a self-contained agent bootstrap package for Linux/macOS.

Run on a build machine; `opencode` is bundled so target machines need no
internet / no manual install. If opencode is not installed locally it is
downloaded automatically from the official releases
(github.com/sst/opencode/releases/latest). Produces:

    <output-dir>/agent-mesh-agent-{linux|darwin}-{x64|arm64}.tar.gz
    <output-dir>/install.sh
    <output-dir>/VERSION

The package contains:
    bin/agent-mesh-edge   PyInstaller-built edge agent binary
    bin/opencode          opencode CLI binary (REQUIRED for llm tasks)
    install.sh            one-command installer
    VERSION

Usage:
    python scripts/build-agent-bootstrap.py
    python scripts/build-agent-bootstrap.py --opencode /path/to/opencode
    python scripts/build-agent-bootstrap.py --output-dir /opt/agent-mesh/data/bootstrap
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_BOOTSTRAP_DIR = PROJECT_ROOT / "data" / "bootstrap"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"

# Official opencode release assets (github.com/sst/opencode). The "latest" URL
# redirects to the newest release, so no API token / rate-limit handling needed.
_OPENCODE_ASSETS = {
    ("linux", "x64"): "opencode-linux-x64.tar.gz",
    ("linux", "arm64"): "opencode-linux-arm64.tar.gz",
    ("darwin", "x64"): "opencode-darwin-x64.zip",
    ("darwin", "arm64"): "opencode-darwin-arm64.zip",
}
_OPENCODE_DOWNLOAD_BASE = os.environ.get(
    "OPENCODE_DOWNLOAD_BASE",
    "https://github.com/sst/opencode/releases/latest/download",
)


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _find_opencode(explicit: str | None = None) -> Path | None:
    """Locate a locally installed opencode CLI, in preference order."""
    candidates: list[Path] = []
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"ERROR: --opencode path does not exist: {explicit}")
        candidates.append(p)
    env_path = os.environ.get("OPENCODE_BIN")
    if env_path:
        candidates.append(Path(env_path))
    which = shutil.which("opencode")
    if which:
        candidates.append(Path(which))
    candidates += [
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".opencode" / "bin" / "opencode.exe",
        Path("/opt/agent-mesh-agent/bin/opencode"),
    ]
    for c in candidates:
        if c and c.exists():
            return c
    return None


def _opencode_download_url(os_name: str, arch: str) -> str | None:
    asset = _OPENCODE_ASSETS.get((os_name, arch))
    if not asset:
        return None
    return f"{_OPENCODE_DOWNLOAD_BASE.rstrip('/')}/{asset}"


def _download_opencode(os_name: str, arch: str) -> Path | None:
    """Download + extract the official opencode binary for the target platform."""
    url = _opencode_download_url(os_name, arch)
    if not url:
        print(f"WARNING: no official opencode asset known for {os_name}/{arch}")
        return None
    cache = BUILD_DIR / "opencode-cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(url).name
    print(f"==> downloading opencode: {url}")
    try:
        with urllib.request.urlopen(url, timeout=300) as resp, open(archive, "wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as e:
        print(f"WARNING: failed to download opencode: {e}")
        return None

    extract = cache / "extract"
    shutil.rmtree(extract, ignore_errors=True)
    extract.mkdir(parents=True)
    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(extract, filter="data")
    except Exception as e:
        print(f"WARNING: failed to extract opencode archive: {e}")
        return None

    for p in sorted(extract.rglob("opencode")):
        if p.is_file():
            os.chmod(p, 0o755)
            return p
    print("WARNING: opencode binary not found inside the downloaded archive")
    return None



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


def build(
    os_name: str | None = None,
    arch: str | None = None,
    opencode: str | None = None,
    output_dir: str | None = None,
    allow_missing_opencode: bool = False,
    download_opencode: bool = True,
) -> Path:
    detected_os, detected_arch = _detect_target()
    os_name = os_name or detected_os
    arch = arch or detected_arch
    bootstrap_dir = Path(output_dir).expanduser().resolve() if output_dir else DEFAULT_BOOTSTRAP_DIR
    bootstrap_dir.mkdir(parents=True, exist_ok=True)

    opencode_path = _find_opencode(opencode)
    if opencode_path is None and download_opencode:
        # Not installed locally -> fetch the official binary for the target platform.
        opencode_path = _download_opencode(os_name, arch)
    if opencode_path is None and not allow_missing_opencode:
        raise SystemExit(
            "ERROR: opencode binary not found and download failed; the probe must "
            "bundle it so target machines can run llm tasks.\n"
            "  Provide one of:\n"
            "    --opencode /path/to/opencode\n"
            "    OPENCODE_BIN=/path/to/opencode environment variable\n"
            "    install opencode so `which opencode` / ~/.opencode/bin/opencode works\n"
            "    ensure this machine can reach github.com/sst/opencode releases\n"
            "  Or pass --allow-missing-opencode to build a probe WITHOUT opencode "
            "(llm tasks will not work on installed nodes)."
        )
    if opencode_path is None:
        print("WARNING: building WITHOUT opencode (--allow-missing-opencode); llm tasks will fail")

    exe = _build_binary()

    with tempfile.TemporaryDirectory(prefix="agent-bootstrap-") as td:
        work = Path(td)
        pkg = work / f"agent-mesh-agent-{os_name}-{arch}"
        pkg.mkdir()
        bin_dir = pkg / "bin"
        bin_dir.mkdir()

        # Copy PyInstaller binary.
        shutil.copy2(exe, bin_dir / "agent-mesh-edge")
        os.chmod(bin_dir / "agent-mesh-edge", 0o755)

        # Copy opencode binary (REQUIRED unless explicitly skipped).
        if opencode_path is not None:
            shutil.copy2(opencode_path, bin_dir / "opencode")
            os.chmod(bin_dir / "opencode", 0o755)
            print(f"bundled opencode from {opencode_path}")

        # Copy install.sh from data/bootstrap (canonical version).
        install_src = PROJECT_ROOT / "data" / "bootstrap" / "install.sh"
        if install_src.exists():
            shutil.copy2(install_src, pkg / "install.sh")
            os.chmod(pkg / "install.sh", 0o755)
        else:
            raise SystemExit(
                f"ERROR: {install_src} not found; cannot build a package without the installer"
            )

        # Write the version manifest into the package and the output dir.
        # The orchestrator reads <data_dir>/bootstrap/VERSION to decide whether an
        # edge agent's reported version is stale (see orchestrator/api/edge.py).
        from agent_mesh.shared.constants import VERSION

        (pkg / "VERSION").write_text(VERSION, encoding="utf-8")
        (bootstrap_dir / "VERSION").write_text(VERSION, encoding="utf-8")
        print(f"wrote VERSION={VERSION} to {bootstrap_dir / 'VERSION'}")

        # MANIFEST.json: lets the edge upgrade path skip re-extracting unchanged
        # large members (notably the ~180MB opencode binary) instead of unpacking
        # the whole archive on every upgrade.
        manifest_files: dict[str, dict[str, object]] = {}
        for rel in ("bin/agent-mesh-edge", "bin/opencode"):
            fp = pkg / rel
            if fp.exists():
                manifest_files[rel] = {
                    "size": fp.stat().st_size,
                    "sha256": _sha256(fp),
                }
        manifest = {"version": VERSION, "files": manifest_files}
        (pkg / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("wrote MANIFEST.json")

        # Build tar.gz.
        tar_name = f"agent-mesh-agent-{os_name}-{arch}.tar.gz"
        tar_path = bootstrap_dir / tar_name
        _run(["tar", "-czf", str(tar_path), "-C", str(work), pkg.name])
        print(f"created {tar_path}")

        # Also keep a copy of install.sh at bootstrap root for direct curl.
        shutil.copy2(pkg / "install.sh", bootstrap_dir / "install.sh")
        return tar_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the agent-mesh edge bootstrap package")
    parser.add_argument("--os", dest="os_name", default=None, help="target OS (linux/darwin)")
    parser.add_argument("--arch", default=None, help="target arch (x64/arm64)")
    parser.add_argument("--opencode", default=None, help="path to the opencode binary to bundle")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="where to write the package (default: <repo>/data/bootstrap)",
    )
    parser.add_argument(
        "--allow-missing-opencode",
        action="store_true",
        help="build even if opencode is missing (llm tasks will not work)",
    )
    parser.add_argument(
        "--no-download-opencode",
        action="store_true",
        help="do not auto-download opencode from GitHub when it is not installed locally",
    )
    args = parser.parse_args()
    build(
        os_name=args.os_name,
        arch=args.arch,
        opencode=args.opencode,
        output_dir=args.output_dir,
        allow_missing_opencode=args.allow_missing_opencode,
        download_opencode=not args.no_download_opencode,
    )


if __name__ == "__main__":
    main()
