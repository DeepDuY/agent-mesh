from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tarfile
import time
from pathlib import Path

from agent_mesh.edge.config_writer import WRAPPER_SCRIPT, get_arch, get_os

logger = logging.getLogger(__name__)

#: Give up (until the next process restart) after this many consecutive failed
#: upgrade attempts, so a faulty package can never cause an infinite loop.
_MAX_UPGRADE_FAILURES = 5
#: Base/backoff cap (seconds) between upgrade attempts after a failure.
_UPGRADE_BACKOFF_BASE_S = 300
_UPGRADE_BACKOFF_MAX_S = 3600


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _tar_root(tar: tarfile.TarFile) -> str:
    for name in tar.getnames():
        part = name.split("/", 1)[0]
        if part:
            return part
    raise RuntimeError("upgrade package is empty")


def _member_present(tar: tarfile.TarFile, name: str) -> bool:
    try:
        tar.getmember(name)
        return True
    except KeyError:
        return False


def _extract_tar_member(
    tar: tarfile.TarFile, name: str, dest: Path, mode: int = 0o755
) -> None:
    """Stream a single member out of the archive (no full extraction)."""
    member = tar.getmember(name)
    src = tar.extractfile(member)
    if src is None:
        raise RuntimeError(f"upgrade package missing {name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        shutil.copyfileobj(src, fh, length=1024 * 1024)
    os.chmod(dest, mode)


def _read_manifest(tar: tarfile.TarFile, root: str) -> dict:
    try:
        fh = tar.extractfile(f"{root}/MANIFEST.json")
    except KeyError:
        return {}
    if fh is None:
        return {}
    try:
        data = json.loads(fh.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _needs_replace(path: Path, info: object) -> bool:
    """Whether `path` differs from the manifest entry (size/sha256)."""
    if not path.exists():
        return True
    if not isinstance(info, dict):
        return True
    try:
        if path.stat().st_size != info.get("size"):
            return True
        expected = info.get("sha256")
        if expected and _sha256_file(path) != expected:
            return True
    except OSError:
        return True
    return False


def cleanup_upgrade_tmp(install_dir: str) -> None:
    """Remove upgrade temp files left by a previously interrupted attempt."""
    install = Path(install_dir)
    for d in (install / ".upgrade", install / "etc" / "upgrade"):
        shutil.rmtree(d, ignore_errors=True)
    for f in (
        install / "bin" / "agent-mesh-edge.bin.new",
        install / "bin" / "opencode.new",
    ):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


class UpgradeMixin:
    """Agent self-upgrade: health confirmation, backoff gate and package swap."""

    async def _maybe_upgrade(self, upgrade: dict | None, has_new_tasks: bool) -> None:
        """Run an upgrade attempt when idle, honouring the failure backoff."""
        if (
            not upgrade
            or self._running
            or has_new_tasks
            or self._upgrade_exhausted
        ):
            return
        interval = (
            min(
                _UPGRADE_BACKOFF_BASE_S * (2 ** self._upgrade_failures),
                _UPGRADE_BACKOFF_MAX_S,
            )
            if self._upgrade_failures
            else 0
        )
        if (time.monotonic() - self._last_upgrade_attempt) < interval:
            return
        self._last_upgrade_attempt = time.monotonic()
        try:
            await self._perform_upgrade(upgrade)
        except Exception as e:
            logger.exception("upgrade failed: %s", e)
        # If we reach here the process was not replaced, so the attempt counts
        # as a failure.
        self._upgrade_failures += 1
        if self._upgrade_failures >= _MAX_UPGRADE_FAILURES:
            self._upgrade_exhausted = True
            logger.error(
                "upgrade disabled after %d consecutive failures; "
                "will retry on the next restart",
                self._upgrade_failures,
            )

    def _confirm_upgrade_healthy(self) -> None:
        """After a successful poll, clear the upgrade rollback markers/backups."""
        install = Path(self.install_dir)
        marker = install / "etc" / "upgrading"
        if not marker.exists():
            # Still clear any stray upgrade temp files (interrupted attempt).
            cleanup_upgrade_tmp(self.install_dir)
            return
        try:
            marker.unlink()
            (install / "etc" / "upgrade-started").unlink(missing_ok=True)
            (install / "bin" / "agent-mesh-edge.bin.old").unlink(missing_ok=True)
            (install / "etc" / "agent_version.bak").unlink(missing_ok=True)
            cleanup_upgrade_tmp(self.install_dir)
            logger.info("upgrade confirmed healthy; removed rollback backup")
        except OSError as e:
            logger.warning("could not clear upgrade marker: %s", e)

    async def _perform_upgrade(self, upgrade: dict) -> bool:
        """Download the package, atomically replace the binary and restart.

        Space/loop safety:
          * temp files live in one fixed dir (``<install>/.upgrade``) that is
            force-cleaned before and after every attempt, so temp files can
            never accumulate and fill the disk;
          * the archive is NOT fully extracted — only the needed members are
            streamed out, and ``opencode`` is skipped when unchanged (saves
            ~180MB of I/O and temp space per upgrade).
        Returns True only when the process was/will be replaced.
        """
        version = upgrade.get("version")
        if not version:
            logger.warning("upgrade skipped: no version in directive")
            return False
        install = Path(self.install_dir)
        bin_dir = install / "bin"
        wrapper = bin_dir / "agent-mesh-edge"
        bin_file = bin_dir / "agent-mesh-edge.bin"
        if not wrapper.exists() or not bin_file.exists():
            logger.warning(
                "upgrade skipped: %s is not an installed layout", install
            )
            return False
        if get_os() == "win32":
            logger.warning("upgrade not supported on win32 yet")
            return False

        os_name = get_os()
        arch = get_arch()
        filename = upgrade.get("filename") or f"agent-mesh-agent-{os_name}-{arch}.tar.gz"

        # Fixed staging dir on the install filesystem; force-clean leftovers from
        # any previously interrupted attempt before starting.
        staging = install / ".upgrade"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        pkg_path = staging / filename
        try:
            url = f"{self.client.base_url}/api/bootstrap/{filename}"
            logger.info("downloading upgrade package %s -> %s", url, pkg_path)
            await self.client.download_to(url, str(pkg_path))

            with tarfile.open(pkg_path, "r:gz") as tar:
                root = _tar_root(tar)
                manifest = _read_manifest(tar, root)
                new_version = str(manifest.get("version") or version)

                # Atomic replace with a rollback copy (`.bin.old`). The running
                # process IS this binary, so it must be swapped via rename
                # (os.replace) rather than overwritten in place, which fails on
                # Linux with "text file busy".
                backup = bin_dir / "agent-mesh-edge.bin.old"
                shutil.copy2(bin_file, backup)
                new_stage = bin_dir / "agent-mesh-edge.bin.new"
                _extract_tar_member(tar, f"{root}/bin/agent-mesh-edge", new_stage)
                os.replace(new_stage, bin_file)

                # opencode: only replace when missing/changed (large member).
                if _member_present(tar, f"{root}/bin/opencode"):
                    installed_oc = bin_dir / "opencode"
                    files = manifest.get("files")
                    info = files.get("bin/opencode") if isinstance(files, dict) else None
                    if _needs_replace(installed_oc, info):
                        opencode_stage = bin_dir / "opencode.new"
                        _extract_tar_member(tar, f"{root}/bin/opencode", opencode_stage)
                        os.replace(opencode_stage, installed_oc)
                        logger.info("opencode updated")
                    else:
                        logger.info("opencode unchanged; skipped extraction")

            # Rollback-aware wrapper (so a crashed new binary does not boot-loop).
            wrapper.write_text(WRAPPER_SCRIPT, encoding="utf-8")
            os.chmod(wrapper, 0o755)

            # Version manifest + pending-rollback markers (rollback in wrapper).
            etc = install / "etc"
            etc.mkdir(parents=True, exist_ok=True)
            version_path = etc / "agent_version"
            if version_path.exists():
                shutil.copy2(version_path, etc / "agent_version.bak")
            version_path.write_text(new_version, encoding="utf-8")
            (etc / "upgrading").touch()
            logger.info("upgrade staged (v%s); restarting", new_version)
        except Exception:
            logger.exception("upgrade failed")
            return False
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        # Restart into the new version. Prefer the service manager: it replaces
        # the process with a *clean* environment. We must NOT self-exec after a
        # successful service-manager restart — a PyInstaller onefile binary that
        # execv's itself inherits the parent's `_PYI_*`/`_MEIPASS` env and aborts
        # with "Security validation failure: unexpected name of application's
        # home directory", which made systemd see a failed start and the wrapper
        # roll back, looping the upgrade forever.
        restarted = False
        try:
            import subprocess

            os_name = get_os()
            if os_name == "linux":
                result = subprocess.run(
                    ["systemctl", "restart", "agent-mesh-edge"],
                    check=False,
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                restarted = result.returncode == 0
            elif os_name == "darwin":
                result = subprocess.run(
                    ["launchctl", "kickstart", "-k", "system/com.agentmesh.edge"],
                    check=False,
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                restarted = result.returncode == 0
        except Exception as e:
            logger.warning("service restart failed (%s); falling back to execv", e)

        if restarted:
            # The service manager should SIGTERM us and start the new binary with
            # a clean environment. Wait to be replaced; if that does not happen,
            # report failure so the caller backs off instead of looping.
            logger.info("service manager restart issued; waiting to be replaced")
            await asyncio.sleep(60)
            logger.error("service manager did not replace this process within 60s")
            return False

        # Fallback for non-service installs (dev mode / plain process). Scrub
        # PyInstaller's onefile env before execv so the new binary starts clean.
        env = os.environ.copy()
        for key in [k for k in env if k.startswith("_PYI_") or k == "_MEIPASS"]:
            env.pop(key, None)
        try:
            os.execve(str(wrapper), [str(wrapper)], env)
            return True
        except OSError:
            logger.exception("restart failed")
            return False
