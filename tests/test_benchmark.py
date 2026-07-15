"""Tests for the benchmark suite harness and its console entry point."""

from __future__ import annotations

from helpers import GateCycleRunner, engine_responses, run

from dev_team.benchmark import (
    DEFAULT_CASES,
    _engine_factory,
    _exit_code,
    main,
    run_benchmark,
)
from dev_team.budget import Budget
from dev_team.engine import DeliveryEngine, EngineConfig
from dev_team.evals import EvalCase, EvalReport, EvalResult
from dev_team.execution import InMemoryWorkspace
from dev_team.models import FeatureRequest
from dev_team.testing import ScriptedRunner


def test_default_cases_are_named_and_nonempty():
    assert DEFAULT_CASES
    assert all(c.name and c.request.title for c in DEFAULT_CASES)


def _passing_factory(case):
    # Mirrors the happy-path engine setup: an in-memory workspace with the
    # gate-cycle runner so a scripted delivery succeeds and the case passes.
    return DeliveryEngine(
        ScriptedRunner(by_system_prompt=engine_responses()),
        workspace=InMemoryWorkspace(),
        command_runner=GateCycleRunner(),
        budget=Budget(),
        config=EngineConfig(commit=False),
    )


def test_run_benchmark_scores_cases():
    case = EvalCase(name="c1", request=FeatureRequest(title="T", description="D"))
    report = run(run_benchmark(_passing_factory, cases=[case]))
    assert isinstance(report, EvalReport)
    assert report.passed == 1 and report.pass_rate == 1.0


def _result(failures):
    case = EvalCase(name="x", request=FeatureRequest(title="t", description="d"))
    return EvalResult(case=case, outcome=None, failures=failures)


def test_exit_code_zero_when_all_pass():
    report = EvalReport(results=[_result([]), _result([])])
    assert _exit_code(report) == 0


def test_exit_code_nonzero_on_any_failure():
    report = EvalReport(results=[_result([]), _result(["run did not succeed"])])
    assert _exit_code(report) == 1


def test_engine_factory_uses_injected_runner():
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    factory = _engine_factory(runner, model=None, budget_usd=3.0)
    engine = factory(EvalCase(name="c", request=FeatureRequest(title="T", description="D")))
    assert isinstance(engine, DeliveryEngine)
    assert engine.budget.limit_usd == 3.0
    assert engine.config.commit is False


def test_main_runs_the_suite_and_returns_an_exit_code(capsys):
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    rc = main(["--budget-usd", "5"], runner=runner)
    assert rc in (0, 1)
    assert "Evals:" in capsys.readouterr().out
