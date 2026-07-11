"""Tests for result rendering."""

from __future__ import annotations

from dev_team.models import (
    Design,
    DeploymentPlan,
    FeatureRequest,
    Plan,
    ProjectResult,
    Review,
    Task,
    TaskResult,
    TaskStatus,
    TestReport,
)
from dev_team.report import (
    delivery_to_dict,
    render_delivery_summary,
    render_summary,
    result_to_dict,
)


def _full_result():
    task = Task(id="T1", title="Build", description="d", status=TaskStatus.DONE)
    tr = TaskResult(
        task=task,
        attempts=1,
        review=Review(approved=True, summary="ok"),
        test_report=TestReport(passed=True, coverage=100.0, summary="ok"),
    )
    return ProjectResult(
        request=FeatureRequest(title="Feature", description="d", constraints=["c"]),
        plan=Plan(summary="the plan"),
        design=Design(overview="the design", tech_stack=["python"]),
        task_results=[tr],
        deployment=DeploymentPlan(
            environment="production",
            summary="ship",
            steps=["a"],
            rollback=["b"],
        ),
    )


def _empty_result():
    return ProjectResult(
        request=FeatureRequest(title="Feature", description="d"),
        plan=Plan(summary="p"),
        design=Design(overview="o"),
        task_results=[],
        deployment=None,
    )


def test_result_to_dict_full():
    data = result_to_dict(_full_result())
    assert data["success"] is True
    assert data["tasks"][0]["review_approved"] is True
    assert data["tasks"][0]["tests_passed"] is True
    assert data["tasks"][0]["coverage"] == 100.0
    assert data["deployment"]["environment"] == "production"
    assert data["request"]["constraints"] == ["c"]


def test_result_to_dict_without_review_or_deployment():
    task = Task(id="T1", title="Build", description="d", status=TaskStatus.FAILED)
    tr = TaskResult(task=task, attempts=1)  # no review / test_report
    result = ProjectResult(
        request=FeatureRequest(title="F", description="d"),
        plan=Plan(summary="p"),
        design=Design(overview="o"),
        task_results=[tr],
        deployment=None,
    )
    data = result_to_dict(result)
    assert data["tasks"][0]["review_approved"] is None
    assert data["tasks"][0]["tests_passed"] is None
    assert data["tasks"][0]["coverage"] is None
    assert data["deployment"] is None


def test_render_summary_full():
    text = render_summary(_full_result())
    assert "SUCCESS" in text
    assert "Stack: python" in text
    assert "✓ T1 Build" in text
    assert "Deployment (production)" in text


def test_render_summary_empty_and_incomplete():
    text = render_summary(_empty_result())
    assert "INCOMPLETE" in text
    assert "(no tasks were produced)" in text
    assert "Stack:" not in text
    assert "Deployment" not in text


def _outcome(**kwargs):
    from dev_team.engine import DeliveryOutcome
    from dev_team.models import Design

    defaults = dict(
        request=FeatureRequest(title="F", description="d"),
        plan_summary="p",
        design=Design(overview="o"),
        task_results=[],
    )
    defaults.update(kwargs)
    return DeliveryOutcome(**defaults)


def test_delivery_to_dict_minimal():
    data = delivery_to_dict(_outcome())
    assert data["success"] is False
    assert data["security_approved"] is None
    assert data["production_ready"] is None
    assert data["committed"] is False


def test_delivery_to_dict_full():
    from dev_team.models import (
        ReliabilityReport,
        SecurityReport,
        Task,
        TaskResult,
        TaskStatus,
    )

    task = Task(id="T1", title="t", description="", status=TaskStatus.DONE)
    outcome = _outcome(
        task_results=[TaskResult(task=task, attempts=1)],
        security=SecurityReport(approved=True, summary="ok"),
        reliability=ReliabilityReport(production_ready=True, summary="ok"),
        committed=True,
        workspace_files=["src/x.py"],
    )
    data = delivery_to_dict(outcome)
    assert data["success"] is True
    assert data["security_approved"] is True
    assert data["production_ready"] is True
    assert data["workspace_files"] == ["src/x.py"]


def test_render_delivery_summary_branches():
    from dev_team.models import (
        ReliabilityReport,
        SecurityReport,
        Task,
        TaskResult,
        TaskStatus,
    )

    empty = render_delivery_summary(_outcome())
    assert "INCOMPLETE" in empty
    assert "(no tasks were produced)" in empty

    task = Task(id="T1", title="t", description="", status=TaskStatus.DONE)
    full = render_delivery_summary(
        _outcome(
            task_results=[TaskResult(task=task, attempts=2)],
            security=SecurityReport(approved=False, summary="findings"),
            reliability=ReliabilityReport(production_ready=False, summary="no"),
            committed=False,
            budget_exhausted=True,
            resumed_task_ids=["T0"],
            workspace_files=["src/x.py"],
        )
    )
    assert "BLOCKED" in full
    assert "NOT READY" in full
    assert "EXHAUSTED" in full
    assert "Resumed from checkpoint: T0" in full
    assert "src/x.py" in full

    good = render_delivery_summary(
        _outcome(
            task_results=[TaskResult(task=task, attempts=1)],
            security=SecurityReport(approved=True, summary="ok"),
            reliability=ReliabilityReport(production_ready=True, summary="ok"),
            committed=True,
        )
    )
    assert "SUCCESS" in good
    assert "approved" in good
    assert "Committed: yes" in good


def test_render_delivery_summary_halted():
    from dev_team.verification import DoDReport, GateResult

    halted = _outcome(
        halted_reason="baseline quality gates are already failing",
        baseline=DoDReport([GateResult("tests", False, "3 legacy failures")]),
    )
    text = render_delivery_summary(halted)
    assert "Halted:" in text
    assert "3 legacy failures" in text
    assert "Tasks:" not in text  # nothing ran; report stops at the halt

    data = delivery_to_dict(halted)
    assert data["halted_reason"].startswith("baseline")
    assert data["baseline_green"] is False


def test_render_delivery_summary_shows_branch():
    from dev_team.models import Task, TaskResult, TaskStatus

    task = Task(id="T1", title="t", description="", status=TaskStatus.DONE)
    text = render_delivery_summary(
        _outcome(task_results=[TaskResult(task=task, attempts=1)], branch="dev-team/login")
    )
    assert "Branch:  dev-team/login" in text


def test_render_delivery_summary_halted_without_baseline():
    text = render_delivery_summary(_outcome(halted_reason="working tree is dirty"))
    assert "Halted:  working tree is dirty" in text
