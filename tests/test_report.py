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
from dev_team.report import render_summary, result_to_dict


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
