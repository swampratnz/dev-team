"""Tests for the DevTeam facade and workflow factory."""

from __future__ import annotations

from helpers import happy_responses, run

from dev_team.config import TeamConfig
from dev_team.models import FeatureRequest
from dev_team.sdk import ClaudeAgentRunner
from dev_team.team import DevTeam, build_workflow
from dev_team.testing import ScriptedRunner
from dev_team.workflow import DevelopmentWorkflow


def test_build_workflow_default_config():
    wf = build_workflow(ScriptedRunner([]))
    assert isinstance(wf, DevelopmentWorkflow)
    assert wf.config.max_task_attempts == 2


def test_build_workflow_custom_config():
    wf = build_workflow(ScriptedRunner([]), config=TeamConfig(max_task_attempts=5))
    assert wf.config.max_task_attempts == 5


def test_devteam_with_injected_runner_runs():
    team = DevTeam(ScriptedRunner(happy_responses(1)))
    result = run(team.develop(FeatureRequest(title="t", description="d")))
    assert result.success is True


def test_devteam_develop_feature_with_constraints():
    team = DevTeam(ScriptedRunner(happy_responses(1)))
    result = run(
        team.develop_feature("Login", "Add login", constraints=["secure"])
    )
    assert result.request.constraints == ["secure"]
    assert result.success is True


def test_devteam_develop_feature_without_constraints():
    team = DevTeam(ScriptedRunner(happy_responses(1)))
    result = run(team.develop_feature("Login", "Add login"))
    assert result.request.constraints == []


def test_devteam_defaults_to_claude_runner():
    team = DevTeam(config=TeamConfig(model="claude-x", working_dir="/srv"))
    assert isinstance(team.runner, ClaudeAgentRunner)
    assert team.runner.default_model == "claude-x"
    assert team.runner.cwd == "/srv"


def test_make_engine_defaults_listener_to_team_listener():
    from dev_team.engine import DeliveryEngine
    from dev_team.execution import InMemoryWorkspace

    events = []
    listener = events.append
    team = DevTeam(ScriptedRunner([]), listener=listener)
    engine = team.make_engine(workspace=InMemoryWorkspace())
    assert isinstance(engine, DeliveryEngine)
    assert engine.listener is listener


def test_make_engine_listener_override():
    from dev_team.execution import InMemoryWorkspace

    other = []
    listener = other.append
    team = DevTeam(ScriptedRunner([]))
    engine = team.make_engine(listener=listener, workspace=InMemoryWorkspace())
    assert engine.listener is listener


def test_deliver_runs_engine():
    from helpers import engine_responses
    from dev_team.budget import Budget
    from dev_team.execution import FakeCommandRunner, InMemoryWorkspace
    from dev_team.models import FeatureRequest
    from dev_team.trace import Tracer

    class _Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            self.t += 1.0
            return self.t

    team = DevTeam(ScriptedRunner(by_system_prompt=engine_responses()))
    outcome = run(
        team.deliver(
            FeatureRequest(title="F", description="d"),
            workspace=InMemoryWorkspace(),
            command_runner=FakeCommandRunner(),
            budget=Budget(),
            tracer=Tracer(clock=_Clock()),
        )
    )
    assert outcome.success is True
