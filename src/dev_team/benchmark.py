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
from typing import List, Optional, Sequence

from .budget import Budget
from .engine import DeliveryEngine, EngineConfig
from .evals import EngineFactory, EvalCase, EvalReport, evaluate
from .execution import LocalWorkspace
from .models import FeatureRequest
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


def default_engine_factory(
    model: Optional[str], budget_usd: Optional[float]
) -> EngineFactory:  # pragma: no cover - real agentic SDK/disk run, CI-only
    """Build real, isolated agentic engines — one temp workspace per case."""

    def factory(case: EvalCase) -> DeliveryEngine:
        return DeliveryEngine(
            ClaudeAgentRunner(default_model=model),
            workspace=LocalWorkspace(tempfile.mkdtemp(prefix=f"bench-{case.name}-")),
            budget=Budget(limit_usd=budget_usd),
            config=EngineConfig(agentic=True, use_branch=False, commit=False),
        )

    return factory


def _engine_factory(
    runner: Optional[AgentRunner], model: Optional[str], budget_usd: Optional[float]
) -> EngineFactory:
    """The factory for a benchmark run.

    An injected ``runner`` (tests, embedding) drives in-memory described-mode
    engines that need no SDK, disk, or credentials; otherwise the real agentic
    factory is used.
    """

    if runner is None:  # pragma: no cover - real path, exercised only in CI
        return default_engine_factory(model, budget_usd)

    def factory(case: EvalCase) -> DeliveryEngine:
        return DeliveryEngine(
            runner,
            budget=Budget(limit_usd=budget_usd),
            config=EngineConfig(commit=False),
        )

    return factory


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
    args = parser.parse_args(argv)
    factory = _engine_factory(runner, args.model, args.budget_usd)
    report = asyncio.run(run_benchmark(factory))
    print(report.render())
    return _exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
