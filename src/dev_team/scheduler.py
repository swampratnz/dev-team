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
        ValueError: If ``max_concurrency`` is less than 1, or two tasks share
            an id (dependencies would be ambiguous and status bookkeeping
            would silently drop one of them).
    """

    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")

    by_id: Dict[str, Task] = {}
    for t in tasks:
        if t.id in by_id:
            raise ValueError(f"duplicate task id in plan: {t.id!r}")
        by_id[t.id] = t
    deps: Dict[str, List[str]] = {
        t.id: [d for d in t.dependencies if d in by_id and d != t.id] for t in tasks
    }

    status: Dict[str, ScheduleStatus] = {}

    def resolve(task_id: str, outcome: ScheduleStatus, error: Optional[str] = None) -> None:
        status[task_id] = outcome
        if listener is not None:
            listener(ScheduledResult(task_id, outcome, error))

    async def run_one(task: Task) -> None:
        # A worker exception fails this task (and cascades to dependants)
        # instead of unwinding the whole run and losing every result.
        error: Optional[str] = None
        try:
            ok = await worker(task)
        except Exception as exc:  # noqa: BLE001 - contain per-task failures
            ok = False
            error = f"{type(exc).__name__}: {exc}"
        resolve(task.id, ScheduleStatus.DONE if ok else ScheduleStatus.FAILED, error)

    # Tasks whose asyncio task has been launched (running or finished). A wave
    # barrier (gather over a whole ready set) would pin every newly-unblocked
    # task behind the *slowest* task in its wave; instead we launch each ready
    # task as capacity frees up and re-scan the moment any in-flight task
    # finishes, so a fast task's dependents start without waiting for a slow
    # sibling. ``max_concurrency`` is enforced by never launching past the
    # bound; result ordering, cascade-skip, and cycle detection are unchanged.
    started: set[str] = set()
    in_flight: set["asyncio.Task[None]"] = set()

    while len(status) < len(tasks):
        # Cascade-skip any not-yet-launched task depending on a failed/skipped
        # one. Doing this first can retire the last tasks without launching.
        newly_skipped = False
        for t in tasks:
            if t.id in status or t.id in started:
                continue
            if any(
                status.get(d) in (ScheduleStatus.FAILED, ScheduleStatus.SKIPPED)
                for d in deps[t.id]
            ):
                resolve(t.id, ScheduleStatus.SKIPPED)
                newly_skipped = True
        if newly_skipped:
            continue

        # Launch every dependency-satisfied task we still have capacity for.
        for t in tasks:
            if len(in_flight) >= max_concurrency:
                break
            if t.id in status or t.id in started:
                continue
            if all(status.get(d) is ScheduleStatus.DONE for d in deps[t.id]):
                in_flight.add(asyncio.ensure_future(run_one(t)))
                started.add(t.id)

        if not in_flight:
            # Nothing running and nothing launchable, yet tasks remain: the
            # rest depend on each other and can never become ready.
            raise DependencyCycleError(
                [t.id for t in tasks if t.id not in status and t.id not in started]
            )

        _, in_flight = await asyncio.wait(
            in_flight, return_when=asyncio.FIRST_COMPLETED
        )

    return [ScheduledResult(t.id, status[t.id]) for t in tasks]
