from agent_mesh.edge.execution import resolve_workdir
from agent_mesh.shared.schemas import Constraints, Task


def _task(workdir: str) -> Task:
    return Task(
        task_id="t-1",
        agent_id="client",
        instruction="echo hi",
        constraints=Constraints(workdir=workdir),
    )


def test_workdir_explicit(tmp_path):
    target = tmp_path / "explicit"
    assert resolve_workdir(_task(str(target)), ".") == target.resolve()
    assert target.exists()


def test_workdir_dot_falls_back_to_isolated_subdir(tmp_path):
    fallback = tmp_path / "fallback"
    expected = (fallback / "tasks" / "t-1").resolve()
    assert resolve_workdir(_task("."), str(fallback)) == expected
    assert expected.exists()
    assert resolve_workdir(_task("."), str(fallback)) == expected  # idempotent


def test_workdir_empty_falls_back_to_isolated_subdir(tmp_path):
    fallback = tmp_path / "fallback-empty"
    expected = (fallback / "tasks" / "t-1").resolve()
    assert resolve_workdir(_task(""), str(fallback)) == expected
    assert expected.exists()


def test_workdir_absolute_not_overridden(tmp_path):
    target = tmp_path / "abs"
    fallback = tmp_path / "fallback-abs"
    assert resolve_workdir(_task(str(target)), str(fallback)) == target.resolve()
    assert not fallback.exists()
