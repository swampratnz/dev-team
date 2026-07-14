"""Tests for domain model behaviour."""

from __future__ import annotations

from dev_team.models import (
    Design,
    FeatureRequest,
    Plan,
    ProjectResult,
    Review,
    ReviewComment,
    Severity,
    Task,
    TaskResult,
    TaskStatus,
)


def _done_task_result(task_id="T1", status=TaskStatus.DONE):
    task = Task(id=task_id, title="t", description="d", status=status)
    return TaskResult(task=task, attempts=1)


def test_review_blocking_comments_filters_by_severity():
    review = Review(
        approved=False,
        summary="s",
        comments=[
            ReviewComment(severity=Severity.INFO, message="a"),
            ReviewComment(severity=Severity.MINOR, message="b"),
            ReviewComment(severity=Severity.MAJOR, message="c"),
            ReviewComment(severity=Severity.CRITICAL, message="d"),
        ],
    )
    messages = [c.message for c in review.blocking_comments]
    assert messages == ["c", "d"]


def test_task_result_succeeded_true_and_false():
    assert _done_task_result(status=TaskStatus.DONE).succeeded is True
    assert _done_task_result(status=TaskStatus.FAILED).succeeded is False


def _project(task_results):
    return ProjectResult(
        request=FeatureRequest(title="t", description="d"),
        plan=Plan(summary="p"),
        design=Design(overview="o"),
        task_results=task_results,
    )


def test_project_result_success_requires_tasks():
    assert _project([]).success is False


def test_project_result_success_all_done():
    result = _project([_done_task_result("T1"), _done_task_result("T2")])
    assert result.success is True
    assert len(result.completed_tasks) == 2
    assert result.failed_tasks == []


def test_project_result_mixed_outcomes():
    result = _project(
        [
            _done_task_result("T1", TaskStatus.DONE),
            _done_task_result("T2", TaskStatus.FAILED),
        ]
    )
    assert result.success is False
    assert len(result.completed_tasks) == 1
    assert len(result.failed_tasks) == 1


def test_project_result_cost_defaults_to_zero_and_is_settable():
    # Additive, backward-compatible: results built the old way still work.
    assert _project([]).cost_usd == 0.0
    priced = ProjectResult(
        request=FeatureRequest(title="t", description="d"),
        plan=Plan(summary="p"),
        design=Design(overview="o"),
        task_results=[],
        cost_usd=4.25,
    )
    assert priced.cost_usd == 4.25


def test_security_report_blocking_findings():
    from dev_team.models import SecurityFinding, SecurityReport, Severity

    report = SecurityReport(
        approved=False,
        summary="s",
        findings=[
            SecurityFinding(Severity.INFO, "c", "d"),
            SecurityFinding(Severity.MAJOR, "c", "d"),
            SecurityFinding(Severity.CRITICAL, "c", "d"),
        ],
    )
    assert len(report.blocking_findings) == 2


def test_security_report_scanner_failed_defaults_falsy():
    from dev_team.models import SecurityReport

    report = SecurityReport(approved=True, summary="ok")
    assert report.scanner_failed is False
    assert report.scanner_error is None
