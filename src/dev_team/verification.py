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

from .execution import CommandRunner, DryRunCommandRunner, Workspace
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
    """The pass/fail outcome of a single gate.

    ``executed`` is ``False`` when the gate did not actually run its command —
    a dry run over a :class:`~dev_team.execution.DryRunCommandRunner`, which
    reports exit 0 without doing anything. ``passed`` stays ``True`` in that
    case (the engine's "no gate blocked this" contract is unchanged), but the
    aggregate summary uses ``executed`` to make clear that nothing was verified.
    """

    name: str
    passed: bool
    detail: str = ""
    executed: bool = True


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
        # A dry-run runner exits 0 without running anything; flag the result as
        # not-executed (its output already says so) so an aggregate "N/N gates
        # passed" cannot pass a dry run off as real verification.
        executed = not isinstance(context.runner, DryRunCommandRunner)
        return GateResult(self.name, result.ok, result.output, executed=executed)


_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _coverage_percent(output: str) -> Optional[float]:
    """Extract the overall coverage percentage from tool output.

    A percentage is trusted only when it sits on a line that also names a
    coverage summary: coverage.py's ``TOTAL`` row (preferred), or failing that
    a ``coverage``-labelled line. A bare percentage anywhere else — a
    slow-test warning, a progress readout, a citation — is ignored, and when no
    anchored line carries a percentage the function fails *closed* (returns
    ``None``) so :class:`CoverageGate` blocks rather than passing on a number
    it grabbed at random. This keeps real coverage.py output working while
    refusing to be fooled by a stray trailing ``%``.
    """

    total: Optional[float] = None
    labelled: Optional[float] = None
    for line in output.splitlines():
        matches = _PERCENT.findall(line)
        if not matches:
            continue
        upper = line.upper()
        if "TOTAL" in upper:
            total = float(matches[-1])
        elif "COVERAGE" in upper:
            labelled = float(matches[-1])
    if total is not None:
        return total
    return labelled


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
        """One-line human summary.

        Dry-run gates (``executed is False``) are counted separately and
        called out, so a report can never claim "N/N gates passed" while
        quietly having run nothing.
        """

        passed = sum(1 for r in self.results if r.passed)
        line = f"{passed}/{len(self.results)} gates passed"
        dry = sum(1 for r in self.results if not r.executed)
        if dry:
            line += f" ({dry} dry-run: not executed)"
        return line


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
