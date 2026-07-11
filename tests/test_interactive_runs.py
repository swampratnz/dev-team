"""Tests for interactive plan review and failure escalation in both engines."""

from __future__ import annotations

import pytest

from helpers import (
    deploy_dict,
    design_dict,
    engine_responses,
    happy_responses,
    impl_dict,
    plan_dict,
    qa_report_dict,
    qa_suite_dict,
    review_dict,
    run,
)
from test_engine import KeyedQueueRunner, _engine

from dev_team.budget import Budget
from dev_team.errors import WorkflowError
from dev_team.interaction import Reply, ScriptedChannel
from dev_team.models import (
    FeatureRequest,
    Review,
    Task,
    TaskResult,
    TaskStatus,
    TestReport,
)
from dev_team.team import DevTeam
from dev_team.testing import ScriptedRunner, json_response


def _request():
    return FeatureRequest(title="Login", description="Add login")


# --- simulation workflow ------------------------------------------------------


def _team(runner, channel, listener=None):
    return DevTeam(runner, listener=listener, interaction=channel)


def test_workflow_plan_approved_proceeds():
    events = []
    channel = ScriptedChannel(script=[Reply(choice="approve")])
    team = _team(ScriptedRunner(happy_responses(1)), channel, listener=events.append)
    result = run(team.develop(_request()))
    assert result.success is True
    assert channel.questions[0].topic == "plan-review"
    assert channel.questions[0].asked_by == "Priya"
    assert any(e.stage == "plan-approved" for e in events)


def test_workflow_plan_revision_requests_new_plan():
    responses = [json_response(plan_dict(1)), json_response(plan_dict(2))]
    responses += [json_response(design_dict())]
    for _ in range(2):
        responses += [
            json_response(impl_dict()),
            json_response(review_dict(True)),
            json_response(qa_report_dict(True)),
        ]
    responses += [json_response(deploy_dict())]
    runner = ScriptedRunner(responses)
    channel = ScriptedChannel(
        script=[Reply(choice="revise", text="split the work"), Reply(choice="approve")]
    )
    team = _team(runner, channel)
    result = run(team.develop(_request()))
    assert result.success is True
    assert len(result.plan.tasks) == 2  # the revised plan was used
    revision_calls = [c for c in runner.calls if "split the work" in c["prompt"]]
    assert revision_calls, "revision feedback reached the product manager"


def test_workflow_plan_revision_empty_text_uses_default_feedback():
    responses = [json_response(plan_dict(1)), json_response(plan_dict(1))]
    responses += happy_responses(1)[1:]
    runner = ScriptedRunner(responses)
    channel = ScriptedChannel(
        script=[Reply(choice="revise"), Reply(choice="approve")]
    )
    result = run(_team(runner, channel).develop(_request()))
    assert result.success is True
    assert any("Revise the plan." in c["prompt"] for c in runner.calls)


def test_workflow_plan_abort_raises():
    events = []
    channel = ScriptedChannel(script=[Reply(choice="abort")])
    team = _team(
        ScriptedRunner([json_response(plan_dict(1))]), channel, listener=events.append
    )
    with pytest.raises(WorkflowError) as excinfo:
        run(team.develop(_request()))
    assert "aborted at plan review" in str(excinfo.value)
    assert any(e.stage == "aborted" for e in events)


def test_workflow_without_channel_never_asks():
    team = DevTeam(ScriptedRunner(happy_responses(1)))
    result = run(team.develop(_request()))
    assert result.success is True  # no channel, no questions, no hang


# --- delivery engine ------------------------------------------------------------


def test_engine_plan_approved_proceeds():
    events = []
    channel = ScriptedChannel(script=[Reply(choice="approve")])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, interaction=channel, listener=events.append)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert channel.questions[0].topic == "plan-review"
    assert any(e.stage == "plan-approved" for e in events)


def test_engine_plan_revised_then_approved():
    mapping = dict(engine_responses())
    mapping = {k: [v] for k, v in mapping.items()}
    mapping["product manager"] = [
        json_response(plan_dict(1)),
        json_response(plan_dict(2)),
    ]
    runner = KeyedQueueRunner(mapping)
    channel = ScriptedChannel(
        script=[Reply(choice="revise", text="split T1"), Reply(choice="approve")]
    )
    engine = _engine(runner, interaction=channel)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert len(outcome.task_results) == 2  # revised plan drove the run
    assert any("split T1" in c["prompt"] for c in runner.calls)


def test_engine_plan_rejected_halts():
    channel = ScriptedChannel(script=[Reply(choice="abort")])
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    engine = _engine(runner, interaction=channel)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert "plan rejected at interactive review" in outcome.halted_reason


def test_engine_failure_escalation_skip_keeps_failure():
    channel = ScriptedChannel(
        script=[Reply(choice="approve"), Reply(choice="skip")]
    )
    runner = ScriptedRunner(by_system_prompt=engine_responses(review=False))
    engine = _engine(runner, interaction=channel)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED
    failure_question = channel.questions[1]
    assert failure_question.topic == "task-failure"
    assert "review:" in failure_question.context


def test_engine_failure_escalation_retry_with_guidance_succeeds():
    mapping = {k: [v] for k, v in engine_responses().items()}
    mapping["code reviewer"] = [
        json_response(review_dict(False)),
        json_response(review_dict(True)),
    ]
    mapping["quality assurance engineer"] = [json_response(qa_suite_dict())]
    runner = KeyedQueueRunner(mapping)
    channel = ScriptedChannel(
        script=[
            Reply(choice="approve"),
            Reply(choice="retry", text="use the existing helper"),
        ]
    )
    from dev_team.engine import EngineConfig

    engine = _engine(
        runner, interaction=channel, config=EngineConfig(max_task_attempts=1)
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2  # one round + one granted retry
    assert any("use the existing helper" in c["prompt"] for c in runner.calls)


def test_engine_failure_escalation_retry_empty_text_default_guidance():
    mapping = {k: [v] for k, v in engine_responses().items()}
    mapping["code reviewer"] = [
        json_response(review_dict(False)),
        json_response(review_dict(True)),
    ]
    runner = KeyedQueueRunner(mapping)
    channel = ScriptedChannel(
        script=[Reply(choice="approve"), Reply(choice="retry")]
    )
    from dev_team.engine import EngineConfig

    engine = _engine(
        runner, interaction=channel, config=EngineConfig(max_task_attempts=1)
    )
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert any("try a different approach" in c["prompt"] for c in runner.calls)


def test_engine_without_channel_fails_task_silently():
    runner = ScriptedRunner(by_system_prompt=engine_responses(review=False))
    engine = _engine(runner)
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED


# --- _escalate_failure unit coverage ---------------------------------------------


def _failed_result(review=None, test_report=None):
    task = Task(id="T1", title="t", description="d")
    return TaskResult(task=task, attempts=2, review=review, test_report=test_report)


def test_escalate_failure_skips_when_budget_exhausted():
    channel = ScriptedChannel()  # would raise if asked
    engine = _engine(
        ScriptedRunner([]), interaction=channel, budget=Budget(limit_usd=0.0)
    )
    guidance = run(engine._escalate_failure(_failed_result().task, _failed_result()))
    assert guidance is None
    assert channel.questions == []


def test_escalate_failure_evidence_includes_tests():
    channel = ScriptedChannel(script=[Reply(choice="skip")])
    engine = _engine(ScriptedRunner([]), interaction=channel)
    report = TestReport(passed=False, coverage=10.0, summary="2 failing")
    run(engine._escalate_failure(_failed_result().task, _failed_result(test_report=report)))
    assert "tests: 2 failing" in channel.questions[0].context


def test_escalate_failure_evidence_placeholder_when_empty():
    channel = ScriptedChannel(script=[Reply(choice="skip")])
    engine = _engine(ScriptedRunner([]), interaction=channel)
    run(engine._escalate_failure(_failed_result().task, _failed_result()))
    assert channel.questions[0].context == "no evidence captured"


def test_escalate_failure_guidance_becomes_major_feedback():
    channel = ScriptedChannel(script=[Reply(choice="retry", text="check nulls")])
    engine = _engine(ScriptedRunner([]), interaction=channel)
    review = Review(approved=False, summary="needs work")
    guidance = run(
        engine._escalate_failure(_failed_result().task, _failed_result(review=review))
    )
    assert guidance.approved is False
    assert "check nulls" in guidance.summary
    assert guidance.comments[0].message == "check nulls"
    assert "review: needs work" in channel.questions[0].context
