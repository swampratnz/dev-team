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


def test_result_to_dict_includes_cost_usd():
    result = _full_result()
    result.cost_usd = 0.5  # metered agent spend for the simulation run
    data = result_to_dict(result)
    assert data["cost_usd"] == 0.5


def test_result_to_dict_cost_usd_defaults_to_zero():
    # A run that recorded nothing still carries the field (default 0.0).
    assert result_to_dict(_full_result())["cost_usd"] == 0.0


def test_render_summary_full():
    text = render_summary(_full_result())
    assert "SUCCESS" in text
    assert "Stack: python" in text
    assert "✓ T1 Build" in text
    assert "Deployment (production)" in text


def test_render_summary_shows_cost():
    result = _full_result()
    result.cost_usd = 0.25
    text = render_summary(result)
    assert "Cost:    $0.2500" in text


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
    assert data["security_scanner_failed"] is None
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
    assert data["security_scanner_failed"] is False
    assert data["production_ready"] is True
    assert data["workspace_files"] == ["src/x.py"]


def test_delivery_to_dict_includes_checks():
    from dev_team.checks import ChecksOutcome

    data = delivery_to_dict(
        _outcome(checks=ChecksOutcome("failure", failed=("test (3.12)", "lint")))
    )
    assert data["checks_state"] == "failure"
    assert data["checks_failed"] == ["test (3.12)", "lint"]


def test_delivery_to_dict_checks_absent_defaults():
    data = delivery_to_dict(_outcome())
    assert data["checks_state"] is None and data["checks_failed"] == []


def test_render_delivery_summary_shows_failed_checks():
    from dev_team.checks import ChecksOutcome

    text = render_delivery_summary(
        _outcome(committed=True, checks=ChecksOutcome("failure", failed=("test (3.12)",)))
    )
    assert "Checks: failure — test (3.12)" in text


def test_render_delivery_summary_shows_passing_checks():
    from dev_team.checks import ChecksOutcome

    lines = render_delivery_summary(
        _outcome(committed=True, checks=ChecksOutcome("success"))
    ).splitlines()
    assert "Checks: success" in lines  # exact line, no failed suffix


def test_delivery_to_dict_scanner_failed():
    from dev_team.models import SecurityReport

    outcome = _outcome(
        security=SecurityReport(
            approved=True, summary="ok", scanner_failed=True, scanner_error="not found"
        ),
    )
    data = delivery_to_dict(outcome)
    assert data["security_scanner_failed"] is True


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
    # today's rendering for a normal (non-scanner-failed) report is
    # byte-identical — the new marker must never appear in the common case.
    assert "Security: approved — ok" in good
    assert "[SCANNER DID NOT RUN]" not in good


def test_render_delivery_summary_marks_scanner_failure():
    from dev_team.models import SecurityReport, Task, TaskResult, TaskStatus

    task = Task(id="T1", title="t", description="", status=TaskStatus.DONE)
    text = render_delivery_summary(
        _outcome(
            task_results=[TaskResult(task=task, attempts=1)],
            security=SecurityReport(
                approved=True, summary="ok", scanner_failed=True, scanner_error="not found"
            ),
        )
    )
    assert "Security: approved — ok [SCANNER DID NOT RUN]" in text


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
    # short detail is shown verbatim — no truncation pointer
    assert "(full detail in .dev_team/events.jsonl)" not in text

    data = delivery_to_dict(halted)
    assert data["halted_reason"].startswith("baseline")
    assert data["baseline_green"] is False


def test_render_delivery_summary_halted_long_detail_points_to_journal():
    # U13.3: detail over 200 chars is truncated and points at the full journal.
    from dev_team.verification import DoDReport, GateResult

    long_detail = "x" * 250
    halted = _outcome(
        halted_reason="baseline quality gates are already failing",
        baseline=DoDReport([GateResult("tests", False, long_detail)]),
    )
    text = render_delivery_summary(halted)
    assert "x" * 200 in text
    assert "x" * 201 not in text  # truncated at 200 chars
    assert "(full detail in .dev_team/events.jsonl)" in text


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


def test_render_delivery_summary_shows_pull_request_when_opened():
    text = render_delivery_summary(
        _outcome(committed=True, pull_request_url="https://github.com/acme/mono/pull/9")
    )
    assert "Pull request: https://github.com/acme/mono/pull/9" in text
    # ...and the line is absent when no PR was opened.
    assert "Pull request:" not in render_delivery_summary(_outcome())


def test_delivery_to_dict_carries_pull_request_url():
    assert delivery_to_dict(_outcome())["pull_request_url"] is None
    assert (
        delivery_to_dict(_outcome(pull_request_url="https://x/pull/1"))["pull_request_url"]
        == "https://x/pull/1"
    )


def test_delivery_to_dict_includes_unverified_claims_when_present():
    from dev_team.models import Documentation

    outcome = _outcome(documentation=Documentation(summary="d", unverified_claims=["docs/x.md: cites 'y.py'"]))
    data = delivery_to_dict(outcome)
    assert data["unverified_claims"] == ["docs/x.md: cites 'y.py'"]


def test_delivery_to_dict_unverified_claims_empty_without_documentation():
    assert delivery_to_dict(_outcome())["unverified_claims"] == []


def test_render_delivery_summary_shows_unverified_claims_when_present():
    from dev_team.models import Documentation

    outcome = _outcome(documentation=Documentation(summary="d", unverified_claims=["bad citation"]))
    text = render_delivery_summary(outcome)
    assert "Unverified doc claims: 1" in text
    assert "bad citation" in text


def test_render_delivery_summary_omits_unverified_claims_when_absent():
    from dev_team.models import Documentation

    empty_docs = render_delivery_summary(_outcome(documentation=Documentation(summary="d")))
    assert "Unverified doc claims" not in empty_docs

    no_docs = render_delivery_summary(_outcome())
    assert "Unverified doc claims" not in no_docs
