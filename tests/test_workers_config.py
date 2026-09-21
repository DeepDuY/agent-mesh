"""Worker configuration semantics.

Only single-process is supported: ``AGENT_MESH_WORKERS>1`` is accepted for
forward compatibility but resolves to a single worker (with a warning). See
docs/known-issues.md §1.
"""

from __future__ import annotations

import logging

from agent_mesh.orchestrator.config import OrchestratorConfig
from agent_mesh.orchestrator.main import _resolve_workers


def test_workers_defaults_to_one(monkeypatch):
    monkeypatch.delenv("AGENT_MESH_WORKERS", raising=False)
    assert OrchestratorConfig().workers == 1


def test_workers_env_override_is_parsed(monkeypatch):
    monkeypatch.setenv("AGENT_MESH_WORKERS", "4")
    assert OrchestratorConfig().workers == 4


def test_resolve_workers_single_is_quiet(caplog):
    config = OrchestratorConfig(workers=1)
    with caplog.at_level(logging.WARNING):
        assert _resolve_workers(config) == 1
    assert caplog.records == []


def test_resolve_workers_multi_is_forced_to_single(caplog):
    config = OrchestratorConfig(workers=4)
    with caplog.at_level(logging.WARNING):
        assert _resolve_workers(config) == 1
    assert any("ignored" in r.message for r in caplog.records)
