"""Tests for the development workflow state machine."""

from __future__ import annotations

from helpers import (
    deploy_dict,
    design_dict,
    happy_responses,
    impl_dict,
    plan_dict,
    review_dict,
    run,
    qa_report_dict,
)

from dev_team.budget import Budget
from dev_team.config import TeamConfig
from dev_team.models import FeatureRequest, TaskStatus, TestReport
from dev_team.sdk import AgentResult
from dev_team.team import build_workflow
from dev_team.testing import ScriptedRunner, json_response


def _request():
    return FeatureRequest(title="Feature", description="Do things")


def _workflow(responses, config=None, listener=None):
    runner = ScriptedRunner(responses)
    return build_workflow(runner, config=config, listener=listener)


def _priced(payload, cost):
    """A canned JSON response carrying a metered cost."""

    return AgentResult(text=json_response(payload), cost_usd=cost, num_turns=1)


def test_qa_prompt_carries_implementation_file_contents():
    runner = ScriptedRunner(happy_responses(1))
    wf = build_workflow(runner)
    run(wf.run(_request()))
    qa_calls = [
        c for c in runner.calls if "quality assurance" in c["system_prompt"]
    ]
    assert qa_calls
    # impl_dict()'s file body ("x = 1") reaches QA, fenced as data.
    assert "x = 1" in qa_calls[0]["prompt"]
    assert '<file-content path="src/x.py">' in qa_calls[0]["prompt"]


def test_happy_path_single_task():
    events = []
    wf = _workflow(happy_responses(1), listener=events.append)
    result = run(wf.run(_request()))
    assert result.success is True
    assert result.task_results[0].task.status is TaskStatus.DONE
    assert result.task_results[0].attempts == 1
    assert result.deployment is not None
    # Workflow emits its own lifecycle events.
    assert any(e.stage == "done" for e in events)


def test_multiple_tasks_in_dependency_order():
    plan = {
        "summary": "s",
        "tasks": [
            {"id": "T2", "title": "second", "description": "", "dependencies": ["T1"]},
            {"id": "T1", "title": "first", "description": "", "dependencies": []},
        ],
    }
    responses = [json_response(plan), json_response(design_dict())]
    for _ in range(2):
        responses.append(json_response(impl_dict()))
        responses.append(json_response(review_dict(True)))
        responses.append(json_response(qa_report_dict(True)))
    responses.append(json_response(deploy_dict()))

    wf = _workflow(responses)
    result = run(wf.run(_request()))
    assert [tr.task.id for tr in result.task_results] == ["T1", "T2"]
    assert result.success is True


def test_review_rejects_then_approves():
    responses = [json_response(plan_dict(1)), json_response(design_dict())]
    responses.append(json_response(impl_dict()))
    responses.append(json_response(review_dict(False)))  # attempt 1 rejected
    responses.append(json_response(impl_dict()))
    responses.append(json_response(review_dict(True)))  # attempt 2 approved
    responses.append(json_response(qa_report_dict(True)))
    responses.append(json_response(deploy_dict()))

    wf = _workflow(responses, config=TeamConfig(max_task_attempts=2))
    result = run(wf.run(_request()))
    assert result.success is True
    assert result.task_results[0].attempts == 2


def test_tests_fail_then_pass():
    responses = [json_response(plan_dict(1)), json_response(design_dict())]
    responses.append(json_response(impl_dict()))
    responses.append(json_response(review_dict(True)))
    responses.append(json_response(qa_report_dict(False)))  # attempt 1 tests fail
    responses.append(json_response(impl_dict()))
    responses.append(json_response(review_dict(True)))
    responses.append(json_response(qa_report_dict(True)))  # attempt 2 tests pass
    responses.append(json_response(deploy_dict()))

    wf = _workflow(responses, config=TeamConfig(max_task_attempts=2))
    result = run(wf.run(_request()))
    assert result.success is True
    assert result.task_results[0].attempts == 2


def test_task_fails_when_review_never_approves():
    responses = [json_response(plan_dict(1)), json_response(design_dict())]
    for _ in range(2):
        responses.append(json_response(impl_dict()))
        responses.append(json_response(review_dict(False)))
    responses.append(json_response(deploy_dict()))

    wf = _workflow(responses, config=TeamConfig(max_task_attempts=2))
    result = run(wf.run(_request()))
    assert result.success is False
    assert result.task_results[0].task.status is TaskStatus.FAILED
    assert result.failed_tasks


def test_task_fails_when_tests_never_pass_single_attempt():
    responses = [json_response(plan_dict(1)), json_response(design_dict())]
    responses.append(json_response(impl_dict()))
    responses.append(json_response(review_dict(True)))
    responses.append(json_response(qa_report_dict(False)))
    responses.append(json_response(deploy_dict()))

    wf = _workflow(responses, config=TeamConfig(max_task_attempts=1))
    result = run(wf.run(_request()))
    assert result.success is False
    assert result.task_results[0].task.status is TaskStatus.FAILED
    assert result.task_results[0].attempts == 1


def test_tests_pass_helper_coverage_threshold():
    wf = _workflow(happy_responses(1), config=TeamConfig(min_coverage=90.0))
    passing = TestReport(passed=True, coverage=95.0, summary="s")
    low_cov = TestReport(passed=True, coverage=80.0, summary="s")
    failing = TestReport(passed=False, coverage=100.0, summary="s")
    assert wf._tests_pass(passing) is True
    assert wf._tests_pass(low_cov) is False
    assert wf._tests_pass(failing) is False


def test_feedback_from_tests_is_blocking():
    wf = _workflow(happy_responses(1))
    review = wf._feedback_from_tests(TestReport(passed=False, coverage=50.0, summary="x"))
    assert review.approved is False
    assert review.blocking_comments
    assert "50%" in review.summary


def test_empty_plan_yields_no_success():
    responses = [json_response(plan_dict(0)), json_response(design_dict())]
    responses.append(json_response(deploy_dict()))
    wf = _workflow(responses)
    result = run(wf.run(_request()))
    assert result.task_results == []
    assert result.success is False


def test_dependent_of_failed_task_is_skipped():
    # T2 depends on T1; T1's review never approves, so T1 FAILs and T2 must be
    # cascade-skipped (no implement/review/test agent calls) rather than run on
    # top of missing work.
    events = []
    plan = {
        "summary": "s",
        "tasks": [
            {"id": "T1", "title": "first", "description": "", "dependencies": []},
            {"id": "T2", "title": "second", "description": "", "dependencies": ["T1"]},
        ],
    }
    responses = [
        json_response(plan),
        json_response(design_dict()),
        json_response(impl_dict()),
        json_response(review_dict(False)),  # T1 rejected -> FAILED (single attempt)
        json_response(deploy_dict()),
    ]
    wf = _workflow(
        responses, config=TeamConfig(max_task_attempts=1), listener=events.append
    )
    result = run(wf.run(_request()))
    assert result.success is False
    by_id = {tr.task.id: tr for tr in result.task_results}
    assert by_id["T1"].task.status is TaskStatus.FAILED
    assert by_id["T1"].attempts == 1
    # T2 skipped: FAILED, no attempts, no agent calls consumed.
    assert by_id["T2"].task.status is TaskStatus.FAILED
    assert by_id["T2"].attempts == 0
    assert any(e.stage == "task-skipped" for e in events)


def test_cost_is_metered_and_surfaced_on_result():
    responses = [
        _priced(plan_dict(1), 0.5),
        _priced(design_dict(), 0.5),
        _priced(impl_dict(), 0.5),
        _priced(review_dict(True), 0.5),
        _priced(qa_report_dict(True), 0.5),
        _priced(deploy_dict(), 0.5),
    ]
    wf = _workflow(responses)
    result = run(wf.run(_request()))
    assert result.success is True
    # Six metered calls at $0.50 each, surfaced on the result.
    assert result.cost_usd == 3.0


def test_budget_stop_is_graceful():
    # Ceiling $2.00. Planning+design+T1 spend $1.75; T1's follow-on T2 trips the
    # ceiling mid-flight; T3 is skipped because the budget is already spent; the
    # deployment call is refused pre-flight — all without crashing the run.
    budget = Budget(limit_usd=2.0)
    responses = [
        _priced(plan_dict(3), 0.25),
        _priced(design_dict(), 0.25),
        _priced(impl_dict(), 0.5),  # T1 impl
        _priced(review_dict(True), 0.5),  # T1 review
        _priced(qa_report_dict(True), 0.25),  # T1 qa -> DONE (spent 1.75)
        _priced(impl_dict(), 0.5),  # T2 impl -> spent 2.25, record trips ceiling
    ]
    wf = build_workflow(ScriptedRunner(responses), budget=budget)
    result = run(wf.run(_request()))
    assert result.success is False
    assert result.cost_usd == 2.25
    assert budget.spent == 2.25
    assert budget.exhausted is True
    by_id = {tr.task.id: tr for tr in result.task_results}
    assert by_id["T1"].task.status is TaskStatus.DONE
    assert by_id["T2"].task.status is TaskStatus.FAILED
    assert by_id["T2"].attempts == 0
    assert by_id["T3"].task.status is TaskStatus.FAILED
    assert by_id["T3"].attempts == 0
    assert result.deployment is None  # never attempted


def test_budget_exhausted_before_planning_returns_empty_result():
    # A zero ceiling refuses the very first agent call; the run still returns a
    # populated (empty) result instead of raising.
    budget = Budget(limit_usd=0.0)
    wf = build_workflow(ScriptedRunner([]), budget=budget)
    result = run(wf.run(_request()))
    assert result.success is False
    assert result.task_results == []
    assert result.plan.summary == ""
    assert result.design.overview == ""
    assert result.cost_usd == 0.0
