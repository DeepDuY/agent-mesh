"""Edge self-upgrade hardening: streaming extraction, manifest skip, cleanup."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from agent_mesh.edge.upgrade import (
    _extract_tar_member,
    _member_present,
    _needs_replace,
    _read_manifest,
    _sha256_file,
    _tar_root,
    cleanup_upgrade_tmp,
)

ROOT = "agent-mesh-agent-linux-x64"


def _make_package(path: Path, binary: bytes = b"BINARY", opencode: bytes = b"OPENCODE") -> dict:
    """Build a bootstrap package and return its manifest."""
    manifest = {
        "version": "9.9.9",
        "files": {
            "bin/agent-mesh-edge": {"size": len(binary), "sha256": _hash(binary)},
            "bin/opencode": {"size": len(opencode), "sha256": _hash(opencode)},
        },
    }
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (
            (f"{ROOT}/VERSION", b"9.9.9"),
            (f"{ROOT}/MANIFEST.json", json.dumps(manifest).encode()),
            (f"{ROOT}/bin/agent-mesh-edge", binary),
            (f"{ROOT}/bin/opencode", opencode),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return manifest


def _hash(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_tar_helpers_stream_members(tmp_path):
    pkg = tmp_path / "pkg.tar.gz"
    manifest = _make_package(pkg)

    with tarfile.open(pkg, "r:gz") as tar:
        assert _tar_root(tar) == ROOT
        assert _read_manifest(tar, ROOT) == manifest
        assert _member_present(tar, f"{ROOT}/bin/opencode")
        assert not _member_present(tar, f"{ROOT}/bin/nope")

        dest = tmp_path / "out" / "edge"
        _extract_tar_member(tar, f"{ROOT}/bin/agent-mesh-edge", dest)
        assert dest.read_bytes() == b"BINARY"
        assert dest.stat().st_mode & 0o111  # executable


def test_needs_replace_uses_size_and_sha(tmp_path):
    f = tmp_path / "opencode"
    assert _needs_replace(f, {"size": 3, "sha256": "x"}) is True  # missing

    data = b"abc"
    f.write_bytes(data)
    assert _needs_replace(f, {"size": 3, "sha256": _hash(data)}) is False
    assert _needs_replace(f, {"size": 4, "sha256": _hash(data)}) is True   # size differs
    assert _needs_replace(f, {"size": 3, "sha256": "deadbeef"}) is True    # sha differs
    # No manifest info -> be conservative and replace.
    assert _needs_replace(f, None) is True


def test_cleanup_upgrade_tmp(tmp_path):
    install = tmp_path / "agent"
    (install / "etc" / "upgrade").mkdir(parents=True)
    (install / "etc" / "upgrade" / "pkg.tar.gz").write_bytes(b"x" * 10)
    (install / ".upgrade").mkdir()
    (install / ".upgrade" / "tmp").write_bytes(b"y")
    (install / "bin").mkdir()
    (install / "bin" / "agent-mesh-edge.bin.new").write_bytes(b"z")
    (install / "bin" / "opencode.new").write_bytes(b"z")

    cleanup_upgrade_tmp(str(install))

    assert not (install / "etc" / "upgrade").exists()
    assert not (install / ".upgrade").exists()
    assert not (install / "bin" / "agent-mesh-edge.bin.new").exists()
    assert not (install / "bin" / "opencode.new").exists()
