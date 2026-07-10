"""Tests for topological task ordering."""

from __future__ import annotations

import pytest

from dev_team.errors import DependencyCycleError
from dev_team.models import Task
from dev_team.ordering import topological_order


def _task(task_id, deps=None):
    return Task(id=task_id, title=task_id, description="", dependencies=deps or [])


def test_empty():
    assert topological_order([]) == []


def test_orders_by_dependency():
    tasks = [_task("B", ["A"]), _task("A"), _task("C", ["B"])]
    ordered = [t.id for t in topological_order(tasks)]
    assert ordered.index("A") < ordered.index("B") < ordered.index("C")


def test_ignores_unknown_and_self_dependencies():
    tasks = [_task("A", ["missing", "A"]), _task("B", ["A"])]
    ordered = [t.id for t in topological_order(tasks)]
    assert ordered == ["A", "B"]


def test_detects_cycle():
    tasks = [_task("A", ["B"]), _task("B", ["A"])]
    with pytest.raises(DependencyCycleError) as excinfo:
        topological_order(tasks)
    assert set(excinfo.value.task_ids) == {"A", "B"}


def test_preserves_input_order_for_independent_tasks():
    tasks = [_task("A"), _task("B"), _task("C")]
    assert [t.id for t in topological_order(tasks)] == ["A", "B", "C"]
