"""An evaluation harness for measuring team quality over time.

"Best in the world" is a benchmark claim, not a vibe: any change to prompts,
roles, or orchestration should be judged by whether it moves a score on a
fixed set of delivery cases. This module provides the minimal harness — a
case describes a feature request and what a successful delivery must produce;
:func:`evaluate` runs each case through a freshly-built engine and scores the
outcome on hard evidence (run success, expected files present, cost).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from .engine import DeliveryEngine, DeliveryOutcome
from .models import FeatureRequest

# A factory builds a fresh, isolated engine per case (own workspace, budget).
EngineFactory = Callable[["EvalCase"], DeliveryEngine]


@dataclass
class EvalCase:
    """One benchmark scenario the team must deliver.

    ``check_commands`` are executed in the delivered workspace after the run
    — behavioural assertions (e.g. ``("python", "-c", "from app import add")``)
    that must exit zero for the case to pass.
    """

    name: str
    request: FeatureRequest
    expected_files: Sequence[str] = ()
    check_commands: Sequence[Sequence[str]] = ()
    require_success: bool = True


@dataclass
class EvalResult:
    """The scored outcome of a single case."""

    case: EvalCase
    outcome: DeliveryOutcome
    failures: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether the case met every expectation."""

        return not self.failures


@dataclass
class EvalReport:
    """Aggregate results across a benchmark run."""

    results: List[EvalResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        """Number of cases that passed."""

        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        """Fraction of cases that passed (0.0 when no cases ran)."""

        if not self.results:
            return 0.0
        return self.passed / len(self.results)

    @property
    def total_cost_usd(self) -> float:
        """Total metered cost across all cases."""

        return sum(r.outcome.cost_usd for r in self.results)

    def render(self) -> str:
        """Render a human-readable scoreboard."""

        lines = [
            f"Evals: {self.passed}/{len(self.results)} passed "
            f"({self.pass_rate:.0%}), total cost ${self.total_cost_usd:.4f}"
        ]
        for result in self.results:
            mark = "✓" if result.passed else "✗"
            lines.append(f"  {mark} {result.case.name}")
            for failure in result.failures:
                lines.append(f"      - {failure}")
        return "\n".join(lines)


def score(
    case: EvalCase,
    outcome: DeliveryOutcome,
    *,
    engine: Optional[DeliveryEngine] = None,
) -> EvalResult:
    """Score ``outcome`` against ``case``'s expectations.

    When ``engine`` is provided, the case's ``check_commands`` are executed in
    the delivered workspace through the engine's (guarded) command runner.
    """

    failures: List[str] = []
    if case.require_success and not outcome.success:
        failures.append("run did not succeed")
    for path in case.expected_files:
        if path not in outcome.workspace_files:
            failures.append(f"expected file missing: {path}")
    if engine is not None:
        for command in case.check_commands:
            result = engine.command_runner.run(list(command), cwd=engine.workdir)
            if not result.ok:
                failures.append(
                    f"check failed ({result.exit_code}): {' '.join(command)}"
                )
    return EvalResult(case=case, outcome=outcome, failures=failures)


async def evaluate(
    engine_factory: EngineFactory,
    cases: Sequence[EvalCase],
) -> EvalReport:
    """Run every case through a fresh engine and return the scored report."""

    report = EvalReport()
    for case in cases:
        engine = engine_factory(case)
        outcome = await engine.deliver(case.request)
        report.results.append(score(case, outcome, engine=engine))
    return report
