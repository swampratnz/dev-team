"""Tests for the evaluation harness."""

from __future__ import annotations

from helpers import engine_responses, run

from dev_team.budget import Budget
from dev_team.engine import DeliveryEngine, EngineConfig
from dev_team.evals import EvalCase, EvalReport, evaluate, score
from dev_team.execution import FakeCommandRunner, InMemoryWorkspace
from dev_team.models import FeatureRequest
from dev_team.testing import ScriptedRunner
from dev_team.trace import Tracer


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def _factory(*, review=True):
    def build(case):
        return DeliveryEngine(
            ScriptedRunner(by_system_prompt=engine_responses(review=review)),
            workspace=InMemoryWorkspace(),
            command_runner=FakeCommandRunner(),
            budget=Budget(),
            tracer=Tracer(clock=_Clock()),
            config=EngineConfig(max_task_attempts=1),
        )

    return build


def _case(name, **kwargs):
    return EvalCase(
        name=name,
        request=FeatureRequest(title="F", description="d"),
        **kwargs,
    )


def test_evaluate_scores_pass_and_fail():
    cases = [
        _case("delivers", expected_files=["src/x.py"]),
        _case("missing-file", expected_files=["nope.py"]),
    ]
    report = run(evaluate(_factory(), cases))
    assert report.passed == 1
    assert report.pass_rate == 0.5
    assert report.total_cost_usd == 0.0
    rendered = report.render()
    assert "✓ delivers" in rendered
    assert "✗ missing-file" in rendered
    assert "expected file missing: nope.py" in rendered


def test_evaluate_failure_when_run_unsuccessful():
    report = run(evaluate(_factory(review=False), [_case("fails")]))
    assert report.passed == 0
    assert report.results[0].failures == ["run did not succeed"]


def test_evaluate_require_success_false_tolerates_failure():
    report = run(
        evaluate(_factory(review=False), [_case("lenient", require_success=False)])
    )
    assert report.passed == 1


def test_empty_report_edges():
    report = EvalReport()
    assert report.pass_rate == 0.0
    assert report.total_cost_usd == 0.0
    assert report.render().startswith("Evals: 0/0")


def test_score_direct():
    case = _case("direct", expected_files=["a.py"])
    engine = _factory()(case)
    outcome = run(engine.deliver(case.request))
    result = score(case, outcome)
    assert result.passed is False
    assert "expected file missing: a.py" in result.failures
