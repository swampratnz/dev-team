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


def test_lint_plan_clean():
    from dev_team.models import Plan
    from dev_team.ordering import lint_plan

    plan = Plan(
        summary="s",
        tasks=[
            Task(id="T1", title="a", description="", acceptance_criteria=["x == 1"]),
            Task(
                id="T2",
                title="b",
                description="",
                acceptance_criteria=["y"],
                dependencies=["T1"],
            ),
        ],
    )
    assert lint_plan(plan) == []


def test_lint_plan_catches_defects():
    from dev_team.models import Plan
    from dev_team.ordering import lint_plan

    plan = Plan(
        summary="s",
        tasks=[
            Task(id="T1", title="a", description=""),  # no criteria
            Task(id="T1", title="dup", description="", acceptance_criteria=["x"]),
            Task(
                id="T3",
                title="c",
                description="",
                acceptance_criteria=["x"],
                dependencies=["T9", "T3"],
            ),
        ],
    )
    issues = lint_plan(plan)
    assert any("no acceptance criteria" in i for i in issues)
    assert any("duplicate task id" in i for i in issues)
    assert any("unknown task 'T9'" in i for i in issues)
    assert any("depends on itself" in i for i in issues)


def test_lint_plan_detects_multi_node_cycle():
    from dev_team.models import Plan
    from dev_team.ordering import lint_plan

    # A -> B -> C -> A: no self-dep, no unknown dep, but unschedulable.
    plan = Plan(
        summary="s",
        tasks=[
            Task(id="A", title="a", description="", acceptance_criteria=["x"],
                 dependencies=["C"]),
            Task(id="B", title="b", description="", acceptance_criteria=["x"],
                 dependencies=["A"]),
            Task(id="C", title="c", description="", acceptance_criteria=["x"],
                 dependencies=["B"]),
        ],
    )
    issues = lint_plan(plan)
    assert any("dependency cycle" in i for i in issues)
    # every cyclic task is named
    cycle_issue = next(i for i in issues if "dependency cycle" in i)
    assert "A" in cycle_issue and "B" in cycle_issue and "C" in cycle_issue


def test_lint_plan_no_false_cycle_from_self_or_unknown_deps():
    from dev_team.models import Plan
    from dev_team.ordering import lint_plan

    # A depends on itself and on an unknown id; neither is a real cycle.
    plan = Plan(
        summary="s",
        tasks=[
            Task(id="A", title="a", description="", acceptance_criteria=["x"],
                 dependencies=["A", "ghost"]),
            Task(id="B", title="b", description="", acceptance_criteria=["x"],
                 dependencies=["A"]),
        ],
    )
    issues = lint_plan(plan)
    assert not any("dependency cycle" in i for i in issues)


def test_lint_plan_empty_and_oversized():
    from dev_team.models import Plan
    from dev_team.ordering import MAX_PLAN_TASKS, lint_plan

    assert lint_plan(Plan(summary="s", tasks=[])) == ["the plan contains no tasks"]
    big = Plan(
        summary="s",
        tasks=[
            Task(id=f"T{i}", title="t", description="", acceptance_criteria=["x"])
            for i in range(MAX_PLAN_TASKS + 1)
        ],
    )
    assert any("split the feature" in i for i in lint_plan(big))
