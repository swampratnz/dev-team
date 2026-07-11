"""Tests for the concurrent, dependency-aware scheduler."""

from __future__ import annotations

import pytest
from helpers import run

from dev_team.errors import DependencyCycleError
from dev_team.models import Task
from dev_team.scheduler import ScheduleStatus, schedule


def _task(tid, deps=None):
    return Task(id=tid, title=tid, description="", dependencies=deps or [])


def _status(results):
    return {r.task_id: r.status for r in results}


def test_empty():
    assert run(schedule([], lambda t: None)) == []


def test_all_succeed_respecting_order():
    order = []

    async def worker(task):
        order.append(task.id)
        return True

    tasks = [_task("B", ["A"]), _task("A"), _task("C", ["B"])]
    results = run(schedule(tasks, worker, max_concurrency=2))
    assert all(r.status is ScheduleStatus.DONE for r in results)
    assert order.index("A") < order.index("B") < order.index("C")


def test_failure_cascades_to_skip():
    async def worker(task):
        return task.id != "A"  # A fails

    tasks = [_task("A"), _task("B", ["A"]), _task("C", ["B"]), _task("D")]
    status = _status(run(schedule(tasks, worker)))
    assert status["A"] is ScheduleStatus.FAILED
    assert status["B"] is ScheduleStatus.SKIPPED
    assert status["C"] is ScheduleStatus.SKIPPED  # transitively skipped
    assert status["D"] is ScheduleStatus.DONE


def test_listener_receives_results():
    seen = []

    async def worker(task):
        return True

    run(schedule([_task("A")], worker, listener=seen.append))
    assert seen[0].task_id == "A"
    assert seen[0].status is ScheduleStatus.DONE


def test_cycle_raises():
    tasks = [_task("A", ["B"]), _task("B", ["A"])]
    with pytest.raises(DependencyCycleError):
        run(schedule(tasks, lambda t: None))


def test_rejects_bad_concurrency():
    with pytest.raises(ValueError):
        run(schedule([_task("A")], lambda t: None, max_concurrency=0))


def test_unknown_and_self_deps_ignored():
    async def worker(task):
        return True

    tasks = [_task("A", ["ghost", "A"])]
    status = _status(run(schedule(tasks, worker)))
    assert status["A"] is ScheduleStatus.DONE


def test_worker_exception_fails_task_not_run():
    tasks = [
        Task(id="T1", title="a", description=""),
        Task(id="T2", title="b", description="", dependencies=["T1"]),
    ]
    results = []

    async def worker(task):
        raise RuntimeError("boom")

    scheduled = run(schedule(tasks, worker, listener=results.append))
    statuses = {r.task_id: r.status for r in scheduled}
    assert statuses["T1"] is ScheduleStatus.FAILED
    assert statuses["T2"] is ScheduleStatus.SKIPPED
    errors = {r.task_id: r.error for r in results}
    assert "RuntimeError: boom" in errors["T1"]
    assert errors["T2"] is None


def test_duplicate_task_ids_are_rejected():
    async def worker(task):  # pragma: no cover - never reached
        return True

    with pytest.raises(ValueError, match="duplicate task id"):
        run(schedule([_task("T1"), _task("T1")], worker))
