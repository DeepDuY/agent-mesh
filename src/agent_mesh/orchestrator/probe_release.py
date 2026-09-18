"""Pull prebuilt probe packages from a GitHub Release into the bootstrap dir.

The edge repo's CI builds `agent-mesh-agent-<os>-<arch>.tar.gz` on native
runners and attaches them to a Release tagged ``probe-v<VERSION>``. This module
downloads those assets (via api.github.com, which works even where github.com
itself is blocked) so the orchestrator can keep serving installs/upgrades from
its own ``data/bootstrap/`` cache.

Used by ``POST /api/bootstrap/sync`` and ``scripts/sync_probe_release.py``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

_PKG_NAME_RE = re.compile(r"^agent-mesh-agent-(linux|darwin|win32)-(x64|arm64)\.tar\.gz$")


def _version_from_tag(tag: str | None) -> str | None:
    if not tag:
        return None
    if tag.startswith("probe-v"):
        return tag[len("probe-v"):] or None
    return tag.lstrip("v") or None


async def sync_release(
    repo: str,
    token: str,
    dest_dir: str | Path,
    *,
    download_timeout_s: float = 1800.0,
) -> dict:
    """Download the latest release's probe packages into ``dest_dir``.

    Returns ``{"repo", "tag", "version", "synced": [{"filename", "size"}]}``.
    Raises ``httpx.HTTPError`` on network / API failures.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    api_headers = {"Accept": "application/vnd.github+json"}
    if token:
        api_headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(
        headers=api_headers, timeout=httpx.Timeout(60.0), follow_redirects=True
    ) as client:
        resp = await client.get(f"{GITHUB_API}/repos/{repo}/releases/latest")
        resp.raise_for_status()
        release = resp.json()
        tag = release.get("tag_name")
        version = _version_from_tag(tag)

        synced: list[dict] = []
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if not _PKG_NAME_RE.match(name):
                continue
            asset_api_url = asset.get("url")
            if not asset_api_url:
                continue
            tmp = dest / f".{name}.part"
            # Stream to a temp file then replace, so a failed/partial download
            # never leaves a corrupt package for a node to fetch.
            async with client.stream(
                "GET",
                asset_api_url,
                headers={"Accept": "application/octet-stream"},
                timeout=httpx.Timeout(download_timeout_s),
            ) as dl:
                dl.raise_for_status()
                with open(tmp, "wb") as fh:
                    async for chunk in dl.aiter_bytes():
                        fh.write(chunk)
            target = dest / name
            tmp.replace(target)
            synced.append({"filename": name, "size": target.stat().st_size})
            logger.info("synced probe package %s (%d bytes)", name, target.stat().st_size)

    if version:
        (dest / "VERSION").write_text(version + "\n", encoding="utf-8")

    return {"repo": repo, "tag": tag, "version": version, "synced": synced}
