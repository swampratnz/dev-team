"""Tests for the role-specialised agents and the agent base class."""

from __future__ import annotations

import pytest
from helpers import (
    design_dict,
    deploy_dict,
    impl_dict,
    plan_dict,
    review_dict,
    run,
    qa_report_dict,
)

from dev_team.agents import (
    ArchitectAgent,
    DevOpsAgent,
    EngineerAgent,
    ProductManagerAgent,
    QAAgent,
    ReviewerAgent,
)
from dev_team.agents.engineer import _feedback_section
from dev_team.errors import AgentResponseError
from dev_team.models import (
    ChangeType,
    Design,
    FeatureRequest,
    FileChange,
    Implementation,
    Plan,
    Review,
    ReviewComment,
    Severity,
    Task,
    TaskStatus,
)
from dev_team.testing import ScriptedRunner, json_response


def _runner(payload):
    return ScriptedRunner([json_response(payload)])


# --- base agent ---------------------------------------------------------


def test_base_agent_emits_events():
    events = []
    agent = ProductManagerAgent(
        _runner(plan_dict()), listener=events.append, model="m"
    )
    run(agent.create_plan(FeatureRequest(title="t", description="d")))
    stages = {e.message for e in events}
    assert "working" in stages
    assert "completed" in stages
    assert all(e.role == "product-manager" for e in events)


def test_ask_json_raises_on_non_json():
    agent = ProductManagerAgent(ScriptedRunner(["not json at all"]))
    with pytest.raises(AgentResponseError) as excinfo:
        run(agent.create_plan(FeatureRequest(title="t", description="d")))
    assert excinfo.value.role == "product-manager"


def test_runner_receives_model_and_system_prompt():
    runner = ScriptedRunner([json_response(plan_dict())])
    agent = ProductManagerAgent(runner, model="claude-x")
    run(agent.create_plan(FeatureRequest(title="t", description="d")))
    call = runner.calls[0]
    assert call["model"] == "claude-x"
    assert "product manager" in call["system_prompt"]


# --- product manager ----------------------------------------------------


def test_manager_with_constraints():
    runner = _runner(plan_dict(2))
    agent = ProductManagerAgent(runner)
    plan = run(
        agent.create_plan(
            FeatureRequest(title="t", description="d", constraints=["fast", "cheap"])
        )
    )
    assert isinstance(plan, Plan)
    assert len(plan.tasks) == 2
    assert "fast" in runner.calls[0]["prompt"]


def test_manager_without_constraints():
    runner = _runner(plan_dict(1))
    agent = ProductManagerAgent(runner)
    run(agent.create_plan(FeatureRequest(title="t", description="d")))
    assert "none" in runner.calls[0]["prompt"]


# --- architect ----------------------------------------------------------


def test_architect_with_tasks():
    agent = ArchitectAgent(_runner(design_dict()))
    plan = Plan(summary="s", tasks=[Task(id="T1", title="A", description="")])
    design = run(agent.design(FeatureRequest(title="t", description="d"), plan))
    assert isinstance(design, Design)
    assert design.components[0].name == "Core"


def test_architect_without_tasks():
    runner = _runner(design_dict())
    agent = ArchitectAgent(runner)
    run(agent.design(FeatureRequest(title="t", description="d"), Plan(summary="s")))
    assert "(no tasks)" in runner.calls[0]["prompt"]


# --- engineer -----------------------------------------------------------


def _task(criteria=None):
    return Task(
        id="T1",
        title="Build",
        description="d",
        acceptance_criteria=criteria or [],
    )


def test_engineer_first_attempt():
    runner = _runner(impl_dict())
    agent = EngineerAgent(runner)
    impl = run(agent.implement(_task(["works"]), Design(overview="o")))
    assert isinstance(impl, Implementation)
    assert impl.files[0].change_type is ChangeType.CREATE
    assert "first attempt" in runner.calls[0]["prompt"]


def test_engineer_with_feedback_comments():
    runner = _runner(impl_dict())
    agent = EngineerAgent(runner)
    feedback = Review(
        approved=False,
        summary="needs work",
        comments=[ReviewComment(severity=Severity.MAJOR, message="fix x")],
    )
    run(agent.implement(_task(), Design(overview="o"), feedback))
    assert "fix x" in runner.calls[0]["prompt"]


def test_feedback_section_variants():
    assert "first attempt" in _feedback_section(None)
    with_comments = _feedback_section(
        Review(
            approved=False,
            summary="s",
            comments=[ReviewComment(severity=Severity.MINOR, message="m")],
        )
    )
    assert "[minor] m" in with_comments
    no_comments = _feedback_section(Review(approved=False, summary="s", comments=[]))
    assert "(no specific comments)" in no_comments


# --- reviewer -----------------------------------------------------------


def test_reviewer_with_files():
    agent = ReviewerAgent(_runner(review_dict(True)))
    impl = Implementation(
        task_id="T1",
        summary="s",
        files=[
            FileChange(path="a.py", change_type=ChangeType.CREATE, summary="adds")
        ],
        notes="looks fine",
    )
    review = run(agent.review(_task(["works"]), impl))
    assert isinstance(review, Review)
    assert review.approved is True


def test_reviewer_without_files_or_notes():
    runner = _runner(review_dict(False))
    agent = ReviewerAgent(runner)
    impl = Implementation(task_id="T1", summary="s", files=[], notes="")
    review = run(agent.review(_task(), impl))
    assert review.approved is False
    assert "(no files reported)" in runner.calls[0]["prompt"]
    assert "(none)" in runner.calls[0]["prompt"]


# --- qa -----------------------------------------------------------------


def test_qa_report():
    agent = QAAgent(_runner(qa_report_dict(True, 100.0)))
    impl = Implementation(task_id="T1", summary="s")
    report = run(agent.test(_task(["works"]), impl))
    assert report.passed is True
    assert report.coverage == 100.0


# --- devops -------------------------------------------------------------


def test_devops_with_stack():
    agent = DevOpsAgent(_runner(deploy_dict()))
    design = Design(overview="o", tech_stack=["python", "docker"])
    plan = run(agent.plan_deployment(FeatureRequest(title="t", description="d"), design))
    assert plan.environment == "production"


def test_devops_without_stack():
    runner = _runner(deploy_dict())
    agent = DevOpsAgent(runner)
    run(
        agent.plan_deployment(
            FeatureRequest(title="t", description="d"), Design(overview="o")
        )
    )
    assert "unspecified" in runner.calls[0]["prompt"]


def test_status_enum_roundtrip():
    # Guards against accidental enum value drift used across prompts.
    assert TaskStatus("done") is TaskStatus.DONE
