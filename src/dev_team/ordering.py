"""Topologically order tasks by their declared dependencies."""

from __future__ import annotations

from typing import Dict, List

from .errors import DependencyCycleError
from .models import Task


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
