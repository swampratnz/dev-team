"""Dependency-aware concurrent task scheduling.

Where the original workflow ran tasks strictly one at a time in topological
order, :func:`schedule` runs independent tasks *concurrently* (bounded by
``max_concurrency``) while still respecting dependencies. Tasks whose
dependencies failed (or were themselves skipped) cascade to ``skipped`` instead
of running.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional

from .errors import DependencyCycleError
from .models import Task


class ScheduleStatus(str, Enum):
    """Terminal status of a scheduled task."""

    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ScheduledResult:
    """The outcome of scheduling a single task."""

    task_id: str
    status: ScheduleStatus
    error: Optional[str] = None


# An async worker returns True on success, False on failure.
Worker = Callable[[Task], Awaitable[bool]]
ResultListener = Callable[[ScheduledResult], None]


async def schedule(
    tasks: List[Task],
    worker: Worker,
    *,
    max_concurrency: int = 4,
    listener: Optional[ResultListener] = None,
) -> List[ScheduledResult]:
    """Run ``tasks`` through ``worker`` respecting dependencies, concurrently.

    Raises:
        DependencyCycleError: If remaining tasks can never become ready.
        ValueError: If ``max_concurrency`` is less than 1.
    """

    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")

    by_id = {t.id: t for t in tasks}
    deps: Dict[str, List[str]] = {
        t.id: [d for d in t.dependencies if d in by_id and d != t.id] for t in tasks
    }

    status: Dict[str, ScheduleStatus] = {}
    semaphore = asyncio.Semaphore(max_concurrency)

    def resolve(task_id: str, outcome: ScheduleStatus, error: Optional[str] = None) -> None:
        status[task_id] = outcome
        if listener is not None:
            listener(ScheduledResult(task_id, outcome, error))

    async def run_one(task: Task) -> None:
        # A worker exception fails this task (and cascades to dependants)
        # instead of unwinding the whole run and losing every result.
        error: Optional[str] = None
        try:
            async with semaphore:
                ok = await worker(task)
        except Exception as exc:  # noqa: BLE001 - contain per-task failures
            ok = False
            error = f"{type(exc).__name__}: {exc}"
        resolve(task.id, ScheduleStatus.DONE if ok else ScheduleStatus.FAILED, error)

    while len(status) < len(tasks):
        pending = [t for t in tasks if t.id not in status]

        # Cascade-skip any task that depends on a failed/skipped task.
        blocked = [
            t
            for t in pending
            if any(
                status.get(d) in (ScheduleStatus.FAILED, ScheduleStatus.SKIPPED)
                for d in deps[t.id]
            )
        ]
        if blocked:
            for task in blocked:
                resolve(task.id, ScheduleStatus.SKIPPED)
            continue

        ready = [
            t
            for t in pending
            if all(status.get(d) is ScheduleStatus.DONE for d in deps[t.id])
        ]
        if not ready:
            raise DependencyCycleError([t.id for t in pending])

        await asyncio.gather(*(run_one(task) for task in ready))

    return [ScheduledResult(t.id, status[t.id]) for t in tasks]
