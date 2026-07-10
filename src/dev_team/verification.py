"""Executable quality gates and a Definition-of-Done board.

Instead of trusting an LLM's self-reported "tests passed, coverage 100%", these
gates run real commands (tests, lint, type-check, coverage, security scan)
through an injected :class:`~dev_team.execution.CommandRunner` and derive
pass/fail from actual exit codes and output. A :class:`DefinitionOfDone`
combines many gates into one merge decision.
"""

from __future__ import annotations

import re
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
        result = context.runner.run(list(self.command), cwd=context.cwd)
        return GateResult(self.name, result.ok, result.output)


_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


@dataclass
class CoverageGate:
    """Runs a coverage command and passes when coverage ≥ ``minimum``."""

    name: str
    command: Sequence[str]
    minimum: float = 100.0

    def evaluate(self, context: GateContext) -> GateResult:
        result = context.runner.run(list(self.command), cwd=context.cwd)
        if not result.ok:
            return GateResult(self.name, False, f"command failed: {result.output}")
        matches = _PERCENT.findall(result.output)
        if not matches:
            return GateResult(self.name, False, "no coverage percentage found")
        coverage = float(matches[-1])
        passed = coverage >= self.minimum
        return GateResult(
            self.name, passed, f"coverage {coverage:.1f}% (min {self.minimum:.1f}%)"
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
