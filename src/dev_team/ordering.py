"""Task-graph utilities: topological ordering and plan linting."""

from __future__ import annotations

from typing import Dict, List

from .errors import DependencyCycleError
from .models import Plan, Task

# Plans beyond this size indicate the feature wasn't scoped, and each extra
# task multiplies cost; the PM should split the feature instead.
MAX_PLAN_TASKS = 12


def lint_plan(plan: Plan) -> List[str]:
    """Return the plan's INVEST-style defects (empty list = clean).

    Checks are structural and deterministic: they catch the failure modes
    that reliably sink downstream agents — missing/duplicate ids, unknown
    dependencies, missing acceptance criteria (QA has nothing to verify),
    and unscoped mega-plans.
    """

    issues: List[str] = []
    if not plan.tasks:
        issues.append("the plan contains no tasks")
        return issues
    if len(plan.tasks) > MAX_PLAN_TASKS:
        issues.append(
            f"the plan has {len(plan.tasks)} tasks (max {MAX_PLAN_TASKS}); "
            "split the feature or merge trivial tasks"
        )
    seen: set[str] = set()
    for task in plan.tasks:
        if task.id in seen:
            issues.append(f"duplicate task id: {task.id}")
        seen.add(task.id)
    ids = {t.id for t in plan.tasks}
    for task in plan.tasks:
        if not task.acceptance_criteria:
            issues.append(
                f"task {task.id} has no acceptance criteria — QA cannot verify it"
            )
        for dep in task.dependencies:
            if dep == task.id:
                issues.append(f"task {task.id} depends on itself")
            elif dep not in ids:
                issues.append(f"task {task.id} depends on unknown task {dep!r}")
    # Self-deps and unknown deps are caught above, but a *multi-node* cycle
    # (A→B→A) is not — and it sinks the run later: schedule() raises
    # DependencyCycleError with no ready tasks left. Surface it here so the
    # PM's revision pass can break the cycle instead.
    cyclic = _tasks_in_cycle(plan.tasks, ids)
    if cyclic:
        issues.append(
            f"tasks form a dependency cycle: {', '.join(cyclic)}"
        )
    return issues


def _tasks_in_cycle(tasks: List[Task], ids: set[str]) -> List[str]:
    """Return the ids of tasks that can never be ordered (in input order).

    Considers only *valid* dependencies (known ids, no self-edges), matching
    how :func:`topological_order` and the scheduler resolve them, so an
    unknown or self dependency never masquerades as a cycle. Uses Kahn's
    algorithm: any task left once no more tasks can be resolved lies on — or
    downstream of — a cycle.
    """

    deps: Dict[str, List[str]] = {
        task.id: [dep for dep in task.dependencies if dep in ids and dep != task.id]
        for task in tasks
    }
    resolved: set[str] = set()
    remaining = [task.id for task in tasks]
    while True:
        ready = [tid for tid in remaining if all(d in resolved for d in deps[tid])]
        if not ready:
            return remaining
        resolved.update(ready)
        remaining = [tid for tid in remaining if tid not in resolved]


def topological_order(tasks: List[Task]) -> List[Task]:
    """Return ``tasks`` ordered so dependencies precede their dependents.

    Dependencies referencing unknown task ids (or a task's own id) are ignored,
    which keeps the ordering robust against imperfect model output. Ties are
    broken by the original input order for determinism.

    Raises:
        DependencyCycleError: If the dependency graph contains a cycle.
    """

    by_id = {task.id: task for task in tasks}
    deps: Dict[str, List[str]] = {
        task.id: [
            dep
            for dep in task.dependencies
            if dep in by_id and dep != task.id
        ]
        for task in tasks
    }

    ordered: List[Task] = []
    resolved: set[str] = set()
    remaining = list(tasks)

    while remaining:
        ready = [t for t in remaining if all(d in resolved for d in deps[t.id])]
        if not ready:
            raise DependencyCycleError([t.id for t in remaining])
        for task in ready:
            ordered.append(task)
            resolved.add(task.id)
        ready_ids = {id(t) for t in ready}
        remaining = [t for t in remaining if id(t) not in ready_ids]

    return ordered
