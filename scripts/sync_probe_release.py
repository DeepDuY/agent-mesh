#!/usr/bin/env python3
"""Pull the latest prebuilt probe packages from a GitHub Release into the
orchestrator's ``data/bootstrap/`` cache.

The edge repo builds the packages on native runners and attaches them to a
Release tagged ``probe-v<VERSION>``. Run this after a release (or from cron) so
new installs/upgrades are served from the local cache without rebuilding.

Usage:
    python scripts/sync_probe_release.py
    python scripts/sync_probe_release.py --repo DeepDuY/agent-mesh-edge --dest data/bootstrap
    python scripts/sync_probe_release.py --token "$GITHUB_TOKEN"   # private repo
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent_mesh.orchestrator.probe_release import sync_release  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="DeepDuY/agent-mesh-edge", help="<owner>/<repo>")
    parser.add_argument("--token", default="", help="GitHub token (needed for a private repo)")
    parser.add_argument("--dest", default="data/bootstrap", help="bootstrap dir to write into")
    args = parser.parse_args()

    result = asyncio.run(sync_release(args.repo, args.token, args.dest))
    print(f"repo={result['repo']} tag={result['tag']} version={result['version']}")
    if not result["synced"]:
        print("WARNING: no matching probe packages found in the latest release")
    for item in result["synced"]:
        print(f"  {item['filename']} ({item['size']:,} bytes)")


if __name__ == "__main__":
    main()
