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

from dev_team.config import TeamConfig
from dev_team.models import FeatureRequest, TaskStatus, TestReport
from dev_team.team import build_workflow
from dev_team.testing import ScriptedRunner, json_response


def _request():
    return FeatureRequest(title="Feature", description="Do things")


def _workflow(responses, config=None, listener=None):
    runner = ScriptedRunner(responses)
    return build_workflow(runner, config=config, listener=listener)


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
