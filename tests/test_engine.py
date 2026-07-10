"""Tests for the real delivery engine."""

from __future__ import annotations

import pytest
from helpers import engine_responses, run

from dev_team.budget import Budget
from dev_team.engine import (
    DeliveryEngine,
    DeliveryOutcome,
    EngineConfig,
    _dod_to_test_report,
    _review_from_dod,
)
from dev_team.execution import CommandResult, FakeCommandRunner, InMemoryWorkspace
from dev_team.models import Design, FeatureRequest, TaskStatus
from dev_team.sdk import AgentResult
from dev_team.testing import ScriptedRunner, json_response
from dev_team.trace import Tracer
from dev_team.verification import DoDReport, GateResult


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


class KeyedQueueRunner:
    """Keyed runner where each key maps to a queue that pops (last repeats)."""

    def __init__(self, mapping):
        self.mapping = {k: list(v) for k, v in mapping.items()}
        self.calls = []

    async def run(self, prompt, *, system_prompt=None, allowed_tools=None, model=None):
        self.calls.append(prompt)
        for key, queue in self.mapping.items():
            if system_prompt and key in system_prompt:
                text = queue.pop(0) if len(queue) > 1 else queue[0]
                return AgentResult(text=text, num_turns=1)
        raise AssertionError(f"no queued response for {system_prompt!r}")


class SeqCommandRunner:
    """Returns a queued sequence of results for pytest; 0 for everything else."""

    def __init__(self, pytest_results):
        self.pytest = list(pytest_results)
        self.calls = []

    def run(self, command, *, cwd=None, timeout=None):
        args = list(command)
        self.calls.append(args)
        if "pytest" in " ".join(args):
            return self.pytest.pop(0) if len(self.pytest) > 1 else self.pytest[0]
        return CommandResult(args, 0, "", "")


def _engine(runner, **kwargs):
    kwargs.setdefault("workspace", InMemoryWorkspace())
    kwargs.setdefault("command_runner", FakeCommandRunner())
    kwargs.setdefault("budget", Budget())
    kwargs.setdefault("tracer", Tracer(clock=_Clock()))
    return DeliveryEngine(runner, **kwargs)


def _request():
    return FeatureRequest(title="Login", description="Add login")


# --- happy path ---------------------------------------------------------


def test_deliver_happy_path():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    ws = InMemoryWorkspace()
    cmd = FakeCommandRunner()
    engine = _engine(runner, workspace=ws, command_runner=cmd)
    outcome = run(engine.deliver(_request()))

    assert outcome.success is True
    assert outcome.tasks_complete is True
    assert outcome.task_results[0].task.status is TaskStatus.DONE
    assert ws.list_files() == ["src/x.py"]  # engineer's file was written for real
    assert outcome.security.approved is True
    assert outcome.documentation is not None
    assert outcome.reliability.production_ready is True
    assert outcome.deployment is not None
    assert outcome.blackboard.decisions[0].id == "ADR-001"
    assert outcome.budget.meter.call_count > 0
    # git add + commit ran through the guarded runner
    assert ["git", "add", "-A"] in cmd.calls


def test_deliver_review_reject_then_approve():
    mapping = dict(
        {
            "product manager": [json_response(__plan())],
            "software architect": [json_response(__design())],
            "senior software engineer": [json_response(__impl())],
            "code reviewer": [json_response(__review(False)), json_response(__review(True))],
            "application security engineer": [json_response(__security())],
            "technical writer": [json_response(__docs())],
            "site reliability engineer": [json_response(__rel())],
            "DevOps engineer": [json_response(__deploy())],
        }
    )
    runner = KeyedQueueRunner(mapping)
    engine = _engine(runner, config=EngineConfig(max_task_attempts=2))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2


def test_deliver_gates_fail_then_pass():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = SeqCommandRunner(
        [CommandResult(["pytest"], 1, "", "fail"), CommandResult(["pytest"], 0, "ok", "")]
    )
    engine = _engine(runner, command_runner=cmd, config=EngineConfig(max_task_attempts=2))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is True
    assert outcome.task_results[0].attempts == 2


def test_deliver_task_fails_when_review_never_approves():
    responses = engine_responses(review=False)
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, config=EngineConfig(max_task_attempts=2))
    outcome = run(engine.deliver(_request()))
    assert outcome.success is False
    assert outcome.task_results[0].task.status is TaskStatus.FAILED


def test_deliver_cascade_skip():
    plan = {
        "summary": "s",
        "tasks": [
            {"id": "T1", "title": "first", "description": "", "dependencies": []},
            {"id": "T2", "title": "second", "description": "", "dependencies": ["T1"]},
        ],
    }
    responses = engine_responses(review=False)
    responses["product manager"] = json_response(plan)
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, config=EngineConfig(max_task_attempts=1))
    outcome = run(engine.deliver(_request()))
    statuses = {tr.task.id: tr.task.status for tr in outcome.task_results}
    assert statuses["T1"] is TaskStatus.FAILED
    assert statuses["T2"] is TaskStatus.FAILED  # skipped -> failed placeholder
    skipped = next(tr for tr in outcome.task_results if tr.task.id == "T2")
    assert skipped.attempts == 0
    assert skipped.implementation is None


def test_deliver_without_commit_skips_git():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    cmd = FakeCommandRunner()
    engine = _engine(runner, command_runner=cmd, config=EngineConfig(commit=False))
    run(engine.deliver(_request()))
    assert not any(c[:2] == ["git", "commit"] for c in cmd.calls)


def test_deliver_security_block_fails_success():
    runner = ScriptedRunner(by_system_prompt=engine_responses(security=False))
    outcome = run(_engine(runner).deliver(_request()))
    assert outcome.tasks_complete is True
    assert outcome.security.approved is False
    assert outcome.success is False


def test_deliver_reliability_block_fails_success():
    runner = ScriptedRunner(by_system_prompt=engine_responses(reliability=False))
    outcome = run(_engine(runner).deliver(_request()))
    assert outcome.reliability.production_ready is False
    assert outcome.success is False


# --- config & pure helpers ---------------------------------------------


@pytest.mark.parametrize("kwargs", [{"max_task_attempts": 0}, {"max_concurrency": 0}])
def test_engine_config_validation(kwargs):
    with pytest.raises(ValueError):
        EngineConfig(**kwargs)


def test_default_construction_uses_defaults():
    # Constructing with no injected collaborators exercises every default.
    engine = DeliveryEngine(ScriptedRunner([]))
    assert engine.workspace is not None
    assert engine.git is not None
    assert engine.budget is not None


def test_dod_to_test_report():
    passing = DoDReport([GateResult("t", True, "")])
    failing = DoDReport([GateResult("t", False, "")])
    assert _dod_to_test_report(passing).passed is True
    assert _dod_to_test_report(passing).coverage == 100.0
    assert _dod_to_test_report(failing).coverage == 0.0


def test_review_from_dod():
    report = DoDReport([GateResult("tests", False, "boom"), GateResult("lint", True, "")])
    review = _review_from_dod(report)
    assert review.approved is False
    assert "tests: boom" in review.comments[0].message


def test_delivery_outcome_property_edges():
    # No tasks -> not complete -> not success; budget None -> zero cost.
    outcome = DeliveryOutcome(
        request=_request(),
        plan_summary="p",
        design=Design(overview="o"),
        task_results=[],
    )
    assert outcome.tasks_complete is False
    assert outcome.success is False
    assert outcome.cost_usd == 0.0


# -- tiny JSON payload builders (kept local to avoid helper churn) --------


def __plan():
    return {
        "summary": "s",
        "tasks": [{"id": "T1", "title": "Core", "description": "d", "dependencies": []}],
    }


def __design():
    return {"overview": "o", "components": [], "tech_stack": ["python"], "risks": []}


def __impl():
    return {
        "summary": "impl",
        "files": [{"path": "a.py", "change_type": "create", "summary": "s", "content": "x"}],
        "notes": "",
    }


def __review(ok):
    return {"approved": ok, "summary": "s", "comments": []}


def __security():
    return {"approved": True, "summary": "ok", "findings": []}


def __docs():
    return {"summary": "d", "sections": []}


def __rel():
    return {"production_ready": True, "summary": "r", "slos": [], "risks": [], "runbook": []}


def __deploy():
    return {"environment": "production", "summary": "s", "steps": [], "rollback": []}
