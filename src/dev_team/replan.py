"""Mutate a plan in response to a failed task (dynamic re-planning primitive).

ROADMAP #3: a real team re-plans when a task gets stuck — it splits the task into
smaller pieces, replaces it with a different approach, or drops it — instead of
just recording the failure and moving on. This module is the mechanical core of
that: a :class:`Replan` decision and a pure :func:`apply_replan` that applies it
to a :class:`~dev_team.models.Plan`.

``apply_replan`` is deliberately decision-source-agnostic: *who* decides the
mutation (the product manager autonomously, or a human over the interaction
channel) and *when* (a bounded post-schedule loop in the delivery engine) is
layered on top. Here we only take a well-formed decision and splice it into the
plan graph, guaranteeing the result is still schedulable (unique ids, no dangling
dependencies, no cycle) or raising :class:`ReplanError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, List

from .errors import DevTeamError
from .models import Plan, Task
from .ordering import lint_plan


class ReplanError(DevTeamError):
    """A plan mutation was rejected as invalid (it would corrupt the plan)."""


class ReplanAction(str, Enum):
    """What to do with a failed task."""

    SPLIT = "split"  # break it into two or more smaller tasks
    REPLACE = "replace"  # swap it for a single different-approach task
    DROP = "drop"  # give up on it and remove it from the plan


#: How many replacement tasks each action must carry. ``None`` = "at least two".
_REQUIRED_REPLACEMENTS = {
    ReplanAction.DROP: 0,
    ReplanAction.REPLACE: 1,
    ReplanAction.SPLIT: None,
}


@dataclass(frozen=True)
class Replan:
    """A decision about one failed task: the action and its replacement tasks.

    ``replacements`` must be empty for :attr:`ReplanAction.DROP`, exactly one for
    :attr:`ReplanAction.REPLACE`, and two or more for :attr:`ReplanAction.SPLIT`.
    Each replacement is a fresh :class:`~dev_team.models.Task`; its upstream
    dependencies are the decision-maker's responsibility (they should carry over
    whatever the failed task depended on), while :func:`apply_replan` handles the
    downstream rewiring and id hygiene.
    """

    action: ReplanAction
    failed_task_id: str
    replacements: List[Task] = field(default_factory=list)
    rationale: str = ""


def _unique_id(candidate: str, taken: set) -> str:
    """Return ``candidate`` (or a ``-N`` suffixed variant) not in ``taken``.

    Matches the engine's own duplicate-id convention (``base-2``, ``base-3``, …)
    so re-planned ids read the same as planning-time deduped ones.
    """

    if candidate not in taken:
        return candidate
    n = 2
    while f"{candidate}-{n}" in taken:
        n += 1
    return f"{candidate}-{n}"


def apply_replan(plan: Plan, decision: Replan) -> Plan:
    """Return a new plan with ``decision`` applied; never mutates ``plan``.

    The failed task is removed and its replacements are spliced in at its
    position. Replacement ids are made unique against the surviving tasks (and
    each other); any dependency — on a surviving task or on the failed task —
    is rewired accordingly: a task that depended on the failed one now depends
    on all of its replacements (on none, for a drop), and a replacement can
    never depend on the task it replaces. The result is linted; a mutation that
    would leave the plan empty, dangling, duplicated, or cyclic raises
    :class:`ReplanError`.
    """

    by_id = {task.id: task for task in plan.tasks}
    if decision.failed_task_id not in by_id:
        raise ReplanError(
            f"cannot re-plan {decision.failed_task_id!r}: no such task in the plan"
        )
    required = _REQUIRED_REPLACEMENTS[decision.action]
    count = len(decision.replacements)
    if required is not None and count != required:
        raise ReplanError(
            f"{decision.action.value} requires {required} replacement task(s), "
            f"got {count}"
        )
    if required is None and count < 2:
        raise ReplanError(
            f"{decision.action.value} requires at least two replacement tasks, "
            f"got {count}"
        )

    # Assign each replacement a unique id (against survivors and prior
    # replacements), remembering the remap so intra-replacement dependencies and
    # the downstream rewiring both point at the final ids.
    survivors = {tid for tid in by_id if tid != decision.failed_task_id}
    taken = set(survivors)
    remap: Dict[str, str] = {}
    for repl in decision.replacements:
        final = _unique_id(repl.id, taken)
        remap[repl.id] = final
        taken.add(final)
    replacement_ids = [remap[r.id] for r in decision.replacements]

    def _rewire(deps: List[str], *, drop_failed: bool) -> List[str]:
        rewired: List[str] = []
        for dep in deps:
            if dep == decision.failed_task_id:
                if not drop_failed:
                    rewired.extend(replacement_ids)  # depend on all replacements
                # else: the failed task is gone — drop the dangling edge
            else:
                rewired.append(remap.get(dep, dep))
        # Preserve order while removing the duplicates a fan-out can introduce.
        return list(dict.fromkeys(rewired))

    built_replacements = [
        replace(
            repl,
            id=remap[repl.id],
            # A replacement can't depend on the task it replaces; other deps
            # (including references to sibling replacements) are remapped.
            dependencies=_rewire(repl.dependencies, drop_failed=True),
        )
        for repl in decision.replacements
    ]

    tasks: List[Task] = []
    for task in plan.tasks:
        if task.id == decision.failed_task_id:
            tasks.extend(built_replacements)  # splice replacements into its slot
            continue
        tasks.append(replace(task, dependencies=_rewire(task.dependencies, drop_failed=False)))

    new_plan = Plan(summary=plan.summary, tasks=tasks)
    issues = lint_plan(new_plan)
    if issues:
        raise ReplanError("re-planned graph is invalid: " + "; ".join(issues))
    return new_plan
