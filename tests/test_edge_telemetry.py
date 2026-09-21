"""Edge telemetry reporting: registry extraction and persistence.

Covers the edge reporting standard (see docs/standards/edge-reporting.md):
  * `extract_telemetry` whitelist filtering of the heartbeat payload.
  * registry-driven heartbeat persistence, including the SYSTEM keep-on-null and
    METRIC overwrite policies (SQLite backend).

(Edge-side distro detection lives with the probe in the separate
`agent-mesh-edge` repository.)
"""

import pytest

from agent_mesh.orchestrator.store.sqlite import SQLiteStore
from agent_mesh.orchestrator.task_store import TaskStore
from agent_mesh.shared.schemas import extract_telemetry, validate_telemetry

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
