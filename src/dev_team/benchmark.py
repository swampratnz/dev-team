"""A standing benchmark suite for the delivery engine.

"Best in field" is a benchmark claim, not a vibe: this runs the real team over a
small, fixed set of feature requests and scores each delivery (see
:mod:`dev_team.evals`), so a prompt or orchestration change can be judged by
whether it still delivers green, security-approved, production-ready results
within budget — trended over time rather than eyeballed.

It is disabled by default in CI (the ``.github/workflows/benchmark.yml`` job is
gated behind the ``RUN_BENCHMARKS`` repository variable). Run it on demand with
the ``dev-team-benchmark`` console entry point; ``--budget-usd`` caps the spend
*per case*, so a runaway case cannot drain the whole pool.
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from .benchmark_history import BenchmarkHistory, BenchmarkRun
from .budget import Budget
from .engine import DeliveryEngine, EngineConfig
from .evals import EngineFactory, EvalCase, EvalReport, evaluate
from .execution import LocalWorkspace
from .models import FeatureRequest
from .scores import _signed_cost
from .sdk import AgentRunner, ClaudeAgentRunner

#: The fixed benchmark cases. Deliberately small and self-contained so a run is
#: cheap; each must deliver a green, security-approved, production-ready result
#: to pass (``require_success``, the default). Richer per-case assertions
#: (expected files, behavioural ``check_commands``) can be added over time.
DEFAULT_CASES: Sequence[EvalCase] = (
    EvalCase(
        name="greeting-helper",
        request=FeatureRequest(
            title="Greeting helper",
            description=(
                "Add a pure-Python function greet(name) that returns "
                "'Hello, <name>!', with unit tests covering a normal name and "
                "an empty string."
            ),
        ),
    ),
    EvalCase(
        name="fizzbuzz",
        request=FeatureRequest(
            title="FizzBuzz",
            description=(
                "Add a function fizzbuzz(n) returning the FizzBuzz string for n "
                "(Fizz for multiples of 3, Buzz for 5, FizzBuzz for both, "
                "otherwise the number as a string), with unit tests."
            ),
        ),
    ),
)


async def run_benchmark(
    engine_factory: EngineFactory,
    cases: Sequence[EvalCase] = DEFAULT_CASES,
) -> EvalReport:
    """Run ``cases`` through fresh engines and return the scored report."""

    return await evaluate(engine_factory, cases)


def _exit_code(report: EvalReport) -> int:
    """Zero only when every case passed — the CI signal for a regression."""

    return 0 if report.passed == len(report.results) else 1


def _record_history(history_file: str, report: EvalReport) -> Optional[str]:
    """Append this run to ``history_file`` and return the trend line, if any.

    Fail-secure: a write failure (e.g. an unwritable path) is caught and
    swallowed rather than propagated — history persistence never blocks or
    changes the benchmark's pass/fail signalling.
    """

    history = BenchmarkHistory(history_file)
    run = BenchmarkRun(
        cases_total=len(report.results),
        cases_passed=report.passed,
        cost_usd=report.total_cost_usd,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    try:
        history.record(run)
    except OSError:
        return None
    delta = history.latest_delta()
    return f"Trend: {delta}" if delta is not None else None


def default_engine_factory(
    model: Optional[str],
    budget_usd: Optional[float],
    *,
    skip_architect: bool = False,
) -> EngineFactory:  # pragma: no cover - real agentic SDK/disk run, CI-only
    """Build real, isolated agentic engines — one temp workspace per case."""

    def factory(case: EvalCase) -> DeliveryEngine:
        return DeliveryEngine(
            ClaudeAgentRunner(default_model=model),
            workspace=LocalWorkspace(tempfile.mkdtemp(prefix=f"bench-{case.name}-")),
            budget=Budget(limit_usd=budget_usd),
            config=EngineConfig(
                agentic=True,
                use_branch=False,
                commit=False,
                skip_architect=skip_architect,
            ),
        )

    return factory


def _engine_factory(
    runner: Optional[AgentRunner],
    model: Optional[str],
    budget_usd: Optional[float],
    *,
    skip_architect: bool = False,
) -> EngineFactory:
    """The factory for a benchmark run.

    An injected ``runner`` (tests, embedding) drives in-memory described-mode
    engines that need no SDK, disk, or credentials; otherwise the real agentic
    factory is used. ``skip_architect`` threads through to every case's
    ``EngineConfig`` — the knob ``--compare-architect-ablation`` flips between
    two otherwise-identical factories.
    """

    if runner is None:  # pragma: no cover - real path, exercised only in CI
        return default_engine_factory(model, budget_usd, skip_architect=skip_architect)

    def factory(case: EvalCase) -> DeliveryEngine:
        return DeliveryEngine(
            runner,
            budget=Budget(limit_usd=budget_usd),
            config=EngineConfig(commit=False, skip_architect=skip_architect),
        )

    return factory


def _mean_attempts(report: EvalReport) -> float:
    """Mean total task attempts per case (0.0 when no cases ran)."""

    if not report.results:
        return 0.0
    total = sum(sum(tr.attempts for tr in r.outcome.task_results) for r in report.results)
    return total / len(report.results)


def _signed_pct(delta: float) -> str:
    """Render a fractional delta as a signed percentage (``+25%`` / ``-10%``)."""

    return f"+{delta:.0%}" if delta > 0 else f"{delta:.0%}"


def _signed_float(delta: float) -> str:
    """Render a float delta with an explicit sign (``+0.50`` / ``-1.00``)."""

    return f"+{delta:.2f}" if delta > 0 else f"{delta:.2f}"


@dataclass
class AblationComparison:
    """The pass-rate/attempts/cost deltas between a normal and an architect-ablated run.

    Both reports come from the same fixed case set through two factories that
    differ only in ``EngineConfig.skip_architect`` — a genuine "with design" vs
    "without design" population, per ``docs/BENCHMARKS.md``'s Architect section.
    """

    baseline: EvalReport
    ablated: EvalReport

    @property
    def baseline_mean_attempts(self) -> float:
        return _mean_attempts(self.baseline)

    @property
    def ablated_mean_attempts(self) -> float:
        return _mean_attempts(self.ablated)

    @property
    def pass_rate_delta(self) -> float:
        """Ablated minus baseline pass rate; negative means design helped."""

        return self.ablated.pass_rate - self.baseline.pass_rate

    @property
    def mean_attempts_delta(self) -> float:
        return self.ablated_mean_attempts - self.baseline_mean_attempts

    @property
    def cost_delta_usd(self) -> float:
        return self.ablated.total_cost_usd - self.baseline.total_cost_usd

    def render(self) -> str:
        """Render a human-readable ablation scoreboard."""

        return "\n".join(
            [
                "Architect ablation comparison (skip_architect=True vs baseline):",
                f"  baseline: {self.baseline.passed}/{len(self.baseline.results)} passed "
                f"({self.baseline.pass_rate:.0%}), mean "
                f"{self.baseline_mean_attempts:.2f} attempt(s)/case, "
                f"total cost ${self.baseline.total_cost_usd:.4f}",
                f"  ablated:  {self.ablated.passed}/{len(self.ablated.results)} passed "
                f"({self.ablated.pass_rate:.0%}), mean "
                f"{self.ablated_mean_attempts:.2f} attempt(s)/case, "
                f"total cost ${self.ablated.total_cost_usd:.4f}",
                "  delta (ablated - baseline): "
                f"pass-rate {_signed_pct(self.pass_rate_delta)}, "
                f"attempts {_signed_float(self.mean_attempts_delta)}, "
                f"cost {_signed_cost(self.cost_delta_usd)}",
            ]
        )


def main(argv: Optional[List[str]] = None, runner: Optional[AgentRunner] = None) -> int:
    """Console entry point for the benchmark suite. Returns a process exit code."""

    parser = argparse.ArgumentParser(
        prog="dev-team-benchmark",
        description="Run the fixed benchmark suite through the delivery engine.",
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        metavar="USD",
        help="Cap the metered spend PER CASE (default: uncapped).",
    )
    parser.add_argument(
        "--model", default=None, help="Model override for the benchmark engines."
    )
    parser.add_argument(
        "--history-file",
        default=None,
        metavar="PATH",
        help=(
            "Append this run's aggregate result to a durable JSON trend file "
            "(default: unset, no disk I/O)."
        ),
    )
    parser.add_argument(
        "--compare-architect-ablation",
        action="store_true",
        help=(
            "Run the fixed case set twice — once normally, once with "
            "EngineConfig.skip_architect=True — and print the pass-rate/"
            "attempts/cost deltas between them. Opt-in: roughly doubles this "
            "invocation's cost (every case runs twice); the default single-"
            "pass run is unaffected."
        ),
    )
    args = parser.parse_args(argv)

    if args.compare_architect_ablation:
        baseline_factory = _engine_factory(runner, args.model, args.budget_usd)
        ablated_factory = _engine_factory(
            runner, args.model, args.budget_usd, skip_architect=True
        )
        baseline_report = asyncio.run(run_benchmark(baseline_factory))
        ablated_report = asyncio.run(run_benchmark(ablated_factory))
        print(AblationComparison(baseline=baseline_report, ablated=ablated_report).render())
        if args.history_file is not None:
            trend = _record_history(args.history_file, baseline_report)
            if trend is not None:
                print(trend)
        return _exit_code(baseline_report)

    factory = _engine_factory(runner, args.model, args.budget_usd)
    report = asyncio.run(run_benchmark(factory))
    print(report.render())
    if args.history_file is not None:
        trend = _record_history(args.history_file, report)
        if trend is not None:
            print(trend)
    return _exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
