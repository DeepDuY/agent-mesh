"""SSE realtime hub + endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_hub_publish_subscribe_and_unsubscribe():
    from agent_mesh.orchestrator.realtime import RealtimeHub

    hub = RealtimeHub()
    first = hub.subscribe()
    second = hub.subscribe()
    hub.publish({"type": "tasks_changed"})
    assert first.get_nowait() == {"type": "tasks_changed"}
    assert second.get_nowait() == {"type": "tasks_changed"}

    hub.unsubscribe(first)
    hub.publish({"type": "agents_changed"})
    assert first.empty()
    assert second.get_nowait() == {"type": "agents_changed"}


def test_hub_drops_when_subscriber_slow():
    from agent_mesh.orchestrator.realtime import RealtimeHub

    hub = RealtimeHub()
    q = hub.subscribe()
    for _ in range(200):  # maxsize is 100; must not raise
        hub.publish({"type": "x"})
    assert q.qsize() == 100


def test_publish_is_noop_without_subscribers():
    from agent_mesh.orchestrator import realtime

    realtime.publish("tasks_changed")  # must not raise


def test_realtime_requires_auth(client: TestClient):
    assert client.get("/api/realtime").status_code == 401
