"""Tests for the plan-mutation primitive (dynamic re-planning core)."""

from __future__ import annotations

import pytest

from dev_team.models import Plan, Task, TaskStatus
from dev_team.replan import Replan, ReplanAction, ReplanError, apply_replan


def _task(tid, *, deps=None, ac=("done",)):
    return Task(
        id=tid,
        title=tid,
        description="d",
        acceptance_criteria=list(ac),
        dependencies=list(deps or []),
    )


def _plan(*tasks):
    return Plan(summary="s", tasks=list(tasks))


def _by_id(plan):
    return {t.id: t for t in plan.tasks}


def test_replace_swaps_the_failed_task_in_place_and_rewires_dependents():
    plan = _plan(_task("T1"), _task("T2", deps=["T1"]), _task("T3", deps=["T2"]))
    new = apply_replan(
        plan,
        Replan(ReplanAction.REPLACE, "T2", [_task("T2b", deps=["T1"])]),
    )
    ids = [t.id for t in new.tasks]
    # spliced into T2's slot, T2 gone
    assert ids == ["T1", "T2b", "T3"]
    # the dependent T3 now points at the replacement, not the removed task
    assert _by_id(new)["T3"].dependencies == ["T2b"]
    # the input plan is untouched
    assert [t.id for t in plan.tasks] == ["T1", "T2", "T3"]


def test_split_fans_a_dependent_onto_all_replacements():
    plan = _plan(_task("T1"), _task("T2", deps=["T1"]))
    new = apply_replan(
        plan,
        Replan(
            ReplanAction.SPLIT,
            "T1",
            [_task("T1a"), _task("T1b", deps=["T1a"])],
        ),
    )
    assert [t.id for t in new.tasks] == ["T1a", "T1b", "T2"]
    # T2 depended on T1 -> now depends on both halves (order preserved, no dups)
    assert _by_id(new)["T2"].dependencies == ["T1a", "T1b"]
    # the intra-split edge survives
    assert _by_id(new)["T1b"].dependencies == ["T1a"]


def test_drop_removes_the_task_and_the_dangling_edge():
    plan = _plan(_task("T1"), _task("T2", deps=["T1"]))
    new = apply_replan(plan, Replan(ReplanAction.DROP, "T1"))
    assert [t.id for t in new.tasks] == ["T2"]
    # T2's now-dangling dependency on the dropped task is removed
    assert _by_id(new)["T2"].dependencies == []


def test_replacement_id_collision_is_deduped():
    plan = _plan(_task("T1"), _task("T2"))
    # the replacement reuses an id already in the plan; it must be renamed, and
    # the dependent rewired to the renamed id.
    new = apply_replan(
        plan,
        Replan(ReplanAction.SPLIT, "T1", [_task("T2"), _task("X")]),
    )
    ids = [t.id for t in new.tasks]
    assert "T2-2" in ids  # collided replacement renamed with the -N convention
    assert ids == ["T2-2", "X", "T2"]


def test_replacement_id_collision_probes_past_the_first_suffix():
    # Both the id and its -2 variant already exist, so dedup must walk to -3.
    plan = _plan(_task("T1"), _task("T2"), _task("T2-2"))
    new = apply_replan(plan, Replan(ReplanAction.REPLACE, "T1", [_task("T2")]))
    assert [t.id for t in new.tasks] == ["T2-3", "T2", "T2-2"]


def test_replacement_cannot_depend_on_the_task_it_replaces():
    plan = _plan(_task("T1"))
    # a replacement that (wrongly) lists the failed task as a dependency: the
    # edge is stripped rather than left dangling.
    new = apply_replan(
        plan,
        Replan(ReplanAction.REPLACE, "T1", [_task("T1b", deps=["T1"])]),
    )
    assert _by_id(new)["T1b"].dependencies == []


def test_unknown_failed_task_is_rejected():
    with pytest.raises(ReplanError, match="no such task"):
        apply_replan(_plan(_task("T1")), Replan(ReplanAction.DROP, "T9"))


@pytest.mark.parametrize(
    "decision",
    [
        Replan(ReplanAction.DROP, "T1", [_task("X")]),  # drop must carry none
        Replan(ReplanAction.REPLACE, "T1", []),  # replace must carry one
        Replan(ReplanAction.REPLACE, "T1", [_task("A"), _task("B")]),  # not two
        Replan(ReplanAction.SPLIT, "T1", [_task("A")]),  # split needs >=2
    ],
)
def test_replacement_count_must_match_the_action(decision):
    with pytest.raises(ReplanError, match="replacement task"):
        apply_replan(_plan(_task("T1"), _task("T2")), decision)


def test_dropping_the_only_task_is_rejected_as_an_empty_plan():
    with pytest.raises(ReplanError, match="no tasks"):
        apply_replan(_plan(_task("T1")), Replan(ReplanAction.DROP, "T1"))


def test_a_mutation_that_forms_a_cycle_is_rejected():
    # T2 depends on T1; replacing T1 with a task that depends on T2 closes a
    # loop, which lint catches before it can sink the scheduler.
    plan = _plan(_task("T1"), _task("T2", deps=["T1"]))
    with pytest.raises(ReplanError, match="cycle"):
        apply_replan(
            plan,
            Replan(ReplanAction.REPLACE, "T1", [_task("T1b", deps=["T2"])]),
        )


def test_replacement_without_acceptance_criteria_is_rejected():
    # lint requires acceptance criteria (QA needs something to verify), so an
    # under-specified replacement is refused rather than silently scheduled.
    with pytest.raises(ReplanError, match="acceptance criteria"):
        apply_replan(
            _plan(_task("T1"), _task("T2")),
            Replan(ReplanAction.REPLACE, "T1", [_task("T1b", ac=())]),
        )


def test_replacements_are_pending_and_carry_their_fields():
    plan = _plan(_task("T1"))
    repl = Task(
        id="T1b", title="new approach", description="try X",
        acceptance_criteria=["passes"], dependencies=[], status=TaskStatus.PENDING,
    )
    new = apply_replan(plan, Replan(ReplanAction.REPLACE, "T1", [repl]))
    got = _by_id(new)["T1b"]
    assert got.title == "new approach" and got.acceptance_criteria == ["passes"]
    assert got.status is TaskStatus.PENDING
