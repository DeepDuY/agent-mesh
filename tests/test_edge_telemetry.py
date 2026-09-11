"""Edge telemetry reporting: distro detection, registry extraction, persistence.

Covers the edge reporting standard (see docs/standards/edge-reporting.md):
  * `_parse_os_release` / `get_distro` mapping of /etc/os-release.
  * `extract_telemetry` whitelist filtering of the heartbeat payload.
  * registry-driven heartbeat persistence, including the SYSTEM keep-on-null and
    METRIC overwrite policies (SQLite backend).
"""

import pytest

from agent_mesh.edge.config_writer import _parse_os_release, get_distro
from agent_mesh.orchestrator.store.sqlite import SQLiteStore
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import extract_telemetry, validate_telemetry

# ----------------------------------------------------------------------
# /etc/os-release parsing
# ----------------------------------------------------------------------
def test_parse_os_release_id_version():
    text = (
        'NAME="Ubuntu"\n'
        'VERSION="22.04.4 LTS (Jammy Jellyfish)"\n'
        'ID=ubuntu\n'
        "ID_LIKE=debian\n"
        'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n'
        "VERSION_ID=\"22.04\"\n"
    )
    assert _parse_os_release(text) == "ubuntu 22.04"


def test_parse_os_release_id_without_version():
    text = 'NAME="CentOS Linux"\nID="centos"\nVERSION_ID="7"\n'
    assert _parse_os_release(text) == "centos 7"


def test_parse_os_release_kylin_unquoted():
    text = "ID=kylin\nVERSION_ID=\"V10\"\n"
    assert _parse_os_release(text) == "kylin V10"


def test_parse_os_release_missing_id():
    assert _parse_os_release('NAME="No ID here"\n') is None
    assert _parse_os_release("") is None


def test_get_distro_non_linux_returns_none(monkeypatch):
    monkeypatch.setattr("agent_mesh.edge.config_writer.platform.system", lambda: "Darwin")
    assert get_distro() is None


def test_get_distro_reads_os_release(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_mesh.edge.config_writer.platform.system", lambda: "Linux")
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
    original_path = __import__("pathlib").Path

    class _FakePath(original_path):
        def __new__(cls, *args, **kwargs):
            if args and args[0] == "/etc/os-release":
                return os_release.__class__(str(os_release))
            return super().__new__(cls, *args, **kwargs)

    monkeypatch.setattr("agent_mesh.edge.config_writer.Path", _FakePath)
    assert get_distro() == "ubuntu 22.04"


# ----------------------------------------------------------------------
# Registry extraction / validation
# ----------------------------------------------------------------------
def test_extract_telemetry_ignores_unknown_fields():
    payload = {
        "agent_id": "n",
        "os": "linux",
        "distro": "ubuntu 22.04",
        "arch": "x64",
        "cpu_percent": 12.5,
        "mem_percent": 33.3,
        "some_future_field": "ignored",
    }
    telemetry = extract_telemetry(payload)
    assert telemetry == {
        "os": "linux",
        "distro": "ubuntu 22.04",
        "arch": "x64",
        "cpu_percent": 12.5,
        "mem_percent": 33.3,
    }
    assert "some_future_field" not in telemetry


def test_extract_telemetry_absent_keys_omitted():
    assert extract_telemetry({"agent_id": "n"}) == {}


def test_validate_telemetry_rejects_unknown():
    with pytest.raises(ValueError):
        validate_telemetry({"bogus": 1})
    validate_telemetry({"os": "linux", "distro": "ubuntu 22.04"})  # ok


# ----------------------------------------------------------------------
# Heartbeat persistence (SQLite backend, keep-on-null via COALESCE)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_heartbeat_persists_distro_sqlite(tmp_path):
    backend = SQLiteStore(str(tmp_path / "db.sqlite"))
    await backend.initialize()
    store = TaskStore(store=backend)
    try:
        await store.heartbeat(
            "node", "dev-1", "opencode", "h", "1.3.0",
            telemetry={"os": "linux", "distro": "centos 7", "arch": "x64",
                       "cpu_percent": 10.0, "mem_percent": 20.0},
        )
        await store.heartbeat(
            "node", "dev-1", "opencode", "h", "1.3.0",
            telemetry={"os": "linux", "cpu_percent": 30.0},
        )
        agent = await store.store.get_agent("dev-1")
        assert agent.distro == "centos 7"      # SYSTEM keep-on-null
        assert agent.arch == "x64"
        assert agent.cpu_percent == 30.0       # METRIC overwritten
        assert agent.mem_percent == 20.0       # absent metric left unchanged
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_heartbeat_rejects_unknown_telemetry(tmp_path):
    backend = SQLiteStore(str(tmp_path / "db.sqlite"))
    await backend.initialize()
    store = TaskStore(store=backend)
    try:
        with pytest.raises(ValueError):
            await store.heartbeat(
                "node", "dev-1", "opencode", "h", "1.3.0",
                telemetry={"bogus_field": 1},
            )
    finally:
        await backend.close()
