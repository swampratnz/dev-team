"""Executable quality gates and a Definition-of-Done board.

Instead of trusting an LLM's self-reported "tests passed, coverage 100%", these
gates run real commands (tests, lint, type-check, coverage, security scan)
through an injected :class:`~dev_team.execution.CommandRunner` and derive
pass/fail from actual exit codes and output. A :class:`DefinitionOfDone`
combines many gates into one merge decision.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Sequence, runtime_checkable

from .execution import CommandRunner, Workspace
from .models import Task


@dataclass
class GateContext:
    """Everything a gate might need to evaluate itself."""

    runner: CommandRunner
    workspace: Optional[Workspace] = None
    task: Optional[Task] = None
    cwd: Optional[str] = None
    timeout: Optional[float] = None


@dataclass
class GateResult:
    """The pass/fail outcome of a single gate."""

    name: str
    passed: bool
    detail: str = ""


@runtime_checkable
class Gate(Protocol):
    """A single quality check that either passes or fails."""

    name: str

    def evaluate(self, context: GateContext) -> GateResult:
        """Evaluate the gate against ``context``."""
        ...


@dataclass
class CommandGate:
    """Passes when a command exits zero."""

    name: str
    command: Sequence[str]

    def evaluate(self, context: GateContext) -> GateResult:
        result = context.runner.run(
            list(self.command), cwd=context.cwd, timeout=context.timeout
        )
        return GateResult(self.name, result.ok, result.output)


_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _coverage_percent(output: str) -> Optional[float]:
    """Extract the overall coverage percentage from tool output.

    Prefers a percentage on a line containing ``TOTAL`` (coverage.py's summary
    row) so that stray percentages in warnings or test names cannot win; falls
    back to the last percentage found anywhere.
    """

    for line in output.splitlines():
        if "TOTAL" in line.upper():
            matches = _PERCENT.findall(line)
            if matches:
                return float(matches[-1])
    matches = _PERCENT.findall(output)
    if matches:
        return float(matches[-1])
    return None


@dataclass
class CoverageGate:
    """Runs a coverage command and passes when coverage ≥ ``minimum``."""

    name: str
    command: Sequence[str]
    minimum: float = 100.0

    def evaluate(self, context: GateContext) -> GateResult:
        result = context.runner.run(
            list(self.command), cwd=context.cwd, timeout=context.timeout
        )
        if not result.ok:
            return GateResult(self.name, False, f"command failed: {result.output}")
        coverage = _coverage_percent(result.output)
        if coverage is None:
            return GateResult(self.name, False, "no coverage percentage found")
        passed = coverage >= self.minimum
        return GateResult(
            self.name, passed, f"coverage {coverage:.1f}% (min {self.minimum:.1f}%)"
        )


@dataclass
class RemoteCIGate:
    """Delegates verification to an external CI system.

    For repositories whose build only runs remotely (a Windows-only MSBuild
    pipeline, a hardware farm), local gates can never be green. This gate
    optionally runs a ``trigger_command`` to kick off the remote build, then
    polls ``status_command`` until it exits zero. Any non-zero exit means
    "not passed yet"; the gate fails when the polls are exhausted or the
    trigger itself fails. Both commands are ordinary
    :class:`~dev_team.execution.CommandRunner` invocations, so any CI with a
    CLI (``az pipelines``, ``gh run``, ``curl`` against a status API) plugs
    in without new code.
    """

    name: str
    status_command: Sequence[str]
    trigger_command: Optional[Sequence[str]] = None
    max_polls: int = 30
    poll_interval_seconds: float = 60.0
    sleep: Callable[[float], None] = time.sleep

    def evaluate(self, context: GateContext) -> GateResult:
        if self.trigger_command is not None:
            trigger = context.runner.run(
                list(self.trigger_command), cwd=context.cwd, timeout=context.timeout
            )
            if not trigger.ok:
                return GateResult(
                    self.name, False, f"remote trigger failed: {trigger.output}"
                )
        last = ""
        for attempt in range(self.max_polls):
            if attempt:
                self.sleep(self.poll_interval_seconds)
            result = context.runner.run(
                list(self.status_command), cwd=context.cwd, timeout=context.timeout
            )
            if result.ok:
                return GateResult(self.name, True, result.output)
            last = result.output
        return GateResult(
            self.name,
            False,
            f"remote verification did not pass within {self.max_polls} poll(s): {last}",
        )


@dataclass
class PredicateGate:
    """A gate backed by an arbitrary predicate over the context."""

    name: str
    predicate: Callable[[GateContext], bool]
    detail: str = ""

    def evaluate(self, context: GateContext) -> GateResult:
        return GateResult(self.name, bool(self.predicate(context)), self.detail)


@dataclass
class DoDReport:
    """Aggregate result of evaluating a Definition of Done."""

    results: List[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only when every gate passed (and at least one gate ran)."""

        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failed_gates(self) -> List[GateResult]:
        """Gates that did not pass."""

        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        """One-line human summary."""

        passed = sum(1 for r in self.results if r.passed)
        return f"{passed}/{len(self.results)} gates passed"


@dataclass
class DefinitionOfDone:
    """An ordered set of gates that together define "done"."""

    gates: List[Gate] = field(default_factory=list)

    def add(self, gate: Gate) -> "DefinitionOfDone":
        """Append ``gate`` and return self for chaining."""

        self.gates.append(gate)
        return self

    def evaluate(self, context: GateContext) -> DoDReport:
        """Evaluate every gate and aggregate the results."""

        return DoDReport(results=[gate.evaluate(context) for gate in self.gates])
