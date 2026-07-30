"""Tests for the benchmark suite harness and its console entry point."""

from __future__ import annotations

import dev_team.benchmark as benchmark_module
from helpers import GateCycleRunner, engine_responses, run

from dev_team.benchmark import (
    DEFAULT_CASES,
    AblationComparison,
    _engine_factory,
    _exit_code,
    main,
    run_benchmark,
)
from dev_team.benchmark_history import BenchmarkHistory
from dev_team.budget import Budget, UsageMeter, UsageRecord
from dev_team.engine import DeliveryEngine, DeliveryOutcome, EngineConfig
from dev_team.evals import EvalCase, EvalReport, EvalResult
from dev_team.execution import InMemoryWorkspace
from dev_team.models import Design, FeatureRequest, SecurityReport, Task, TaskResult, TaskStatus
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


def test_main_without_history_file_touches_no_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    main(["--budget-usd", "1"], runner=runner)
    assert list(tmp_path.iterdir()) == []


def test_main_history_file_creates_new_trail(tmp_path):
    history_path = tmp_path / "history.json"
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    main(["--history-file", str(history_path)], runner=runner)
    assert len(BenchmarkHistory(str(history_path)).load()) == 1


def test_main_history_file_appends_and_prints_delta(tmp_path, capsys):
    history_path = tmp_path / "history.json"
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    main(["--history-file", str(history_path)], runner=runner)
    capsys.readouterr()  # discard the first run's output
    main(["--history-file", str(history_path)], runner=runner)
    out = capsys.readouterr().out
    assert "Trend:" in out
    assert len(BenchmarkHistory(str(history_path)).load()) == 2


def test_main_history_write_failure_never_changes_exit_code(tmp_path):
    runner_a = ScriptedRunner(by_system_prompt=engine_responses())
    rc_without = main(["--budget-usd", "5"], runner=runner_a)

    # A directory in place of the history file makes the write fail
    # (IsADirectoryError, an OSError subclass); this must be swallowed rather
    # than raised, and must not change the reported exit code.
    runner_b = ScriptedRunner(by_system_prompt=engine_responses())
    rc_with_failure = main(
        ["--budget-usd", "5", "--history-file", str(tmp_path)], runner=runner_b
    )
    assert rc_with_failure == rc_without


# --- architect ablation compare mode (--compare-architect-ablation, #267) ---


def _spy_evaluate(monkeypatch):
    """Wrap dev_team.benchmark.evaluate to record every (factory, cases) call."""

    calls = []
    original = benchmark_module.evaluate

    async def spy(factory, cases):
        calls.append(factory)
        return await original(factory, cases)

    monkeypatch.setattr(benchmark_module, "evaluate", spy)
    return calls


def test_main_default_invocation_calls_evaluate_exactly_once(monkeypatch):
    calls = _spy_evaluate(monkeypatch)
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    main(["--budget-usd", "5"], runner=runner)
    assert len(calls) == 1
    engine = calls[0](DEFAULT_CASES[0])
    assert engine.config.skip_architect is False


def test_main_compare_architect_ablation_calls_evaluate_exactly_twice(monkeypatch, capsys):
    calls = _spy_evaluate(monkeypatch)
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    rc = main(["--compare-architect-ablation"], runner=runner)
    assert len(calls) == 2
    engines = [factory(DEFAULT_CASES[0]) for factory in calls]
    assert [e.config.skip_architect for e in engines] == [False, True]
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert "Architect ablation comparison" in out
    assert "baseline:" in out
    assert "ablated:" in out
    assert "delta (ablated - baseline)" in out


def test_main_compare_architect_ablation_records_baseline_history_only(tmp_path):
    history_path = tmp_path / "history.json"
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    main(["--compare-architect-ablation", "--history-file", str(history_path)], runner=runner)
    # one entry: the ablated run is a comparison artifact, not a persisted trend point
    assert len(BenchmarkHistory(str(history_path)).load()) == 1


def test_main_compare_architect_ablation_history_prints_trend_on_second_run(
    tmp_path, capsys
):
    history_path = tmp_path / "history.json"
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    main(["--compare-architect-ablation", "--history-file", str(history_path)], runner=runner)
    capsys.readouterr()  # discard the first run's output
    main(["--compare-architect-ablation", "--history-file", str(history_path)], runner=runner)
    out = capsys.readouterr().out
    assert "Trend:" in out
    assert len(BenchmarkHistory(str(history_path)).load()) == 2


def _fake_result(name, attempts, cost_usd, *, success=True):
    task = Task(id="T1", title="t", description="d", acceptance_criteria=["ok"])
    task.status = TaskStatus.DONE if success else TaskStatus.FAILED
    outcome = DeliveryOutcome(
        request=FeatureRequest(title=name, description="d"),
        plan_summary="p",
        design=Design(overview=""),
        task_results=[TaskResult(task=task, attempts=attempts)],
        security=SecurityReport(approved=True, summary="ok"),
        budget=Budget(meter=UsageMeter(records=[UsageRecord("engineer", cost_usd, 1)])),
    )
    case = EvalCase(name=name, request=outcome.request)
    failures = [] if success else ["run did not succeed"]
    return EvalResult(case=case, outcome=outcome, failures=failures)


def test_ablation_comparison_computes_pass_rate_attempts_and_cost_deltas():
    # fixed, fabricated fixtures — no real LLM/agentic calls: baseline passes
    # both cases in 1 attempt each at low cost; ablated fails one case and
    # takes more attempts at higher cost, modelling design's downstream value.
    baseline = EvalReport(
        results=[
            _fake_result("a", attempts=1, cost_usd=1.0),
            _fake_result("b", attempts=1, cost_usd=1.0),
        ]
    )
    ablated = EvalReport(
        results=[
            _fake_result("a", attempts=2, cost_usd=1.5),
            _fake_result("b", attempts=3, cost_usd=2.5, success=False),
        ]
    )
    comparison = AblationComparison(baseline=baseline, ablated=ablated)

    assert comparison.baseline_mean_attempts == 1.0
    assert comparison.ablated_mean_attempts == 2.5
    assert comparison.mean_attempts_delta == 1.5
    assert comparison.pass_rate_delta == 0.5 - 1.0
    assert comparison.cost_delta_usd == 4.0 - 2.0

    rendered = comparison.render()
    assert "baseline: 2/2 passed (100%)" in rendered
    assert "ablated:  1/2 passed (50%)" in rendered
    assert "pass-rate -50%" in rendered
    assert "attempts +1.50" in rendered
    assert "cost +$2.0000" in rendered


def test_ablation_comparison_renders_empty_reports_without_crashing():
    comparison = AblationComparison(baseline=EvalReport(), ablated=EvalReport())
    assert comparison.baseline_mean_attempts == 0.0
    assert comparison.ablated_mean_attempts == 0.0
    assert comparison.pass_rate_delta == 0.0
    assert "0/0 passed" in comparison.render()
