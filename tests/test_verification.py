"""Tests for executable quality gates and the Definition of Done."""

from __future__ import annotations

from dev_team.execution import CommandResult, DryRunCommandRunner, FakeCommandRunner
from dev_team.verification import (
    CommandGate,
    CoverageGate,
    DefinitionOfDone,
    GateContext,
    PredicateGate,
    RemoteCIGate,
)


def _ctx(runner):
    return GateContext(runner=runner)


def test_command_gate_pass_and_fail():
    ok = FakeCommandRunner().add_rule("pytest", CommandResult(["pytest"], 0, "passed", ""))
    bad = FakeCommandRunner().add_rule("pytest", CommandResult(["pytest"], 1, "", "boom"))
    assert CommandGate("tests", ["pytest"]).evaluate(_ctx(ok)).passed is True
    result = CommandGate("tests", ["pytest"]).evaluate(_ctx(bad))
    assert result.passed is False
    assert "boom" in result.detail


def test_coverage_gate_pass():
    runner = FakeCommandRunner().add_rule(
        "cov", CommandResult(["cov"], 0, "TOTAL 100%", "")
    )
    result = CoverageGate("coverage", ["cov"], minimum=100.0).evaluate(_ctx(runner))
    assert result.passed is True
    assert "100.0%" in result.detail


def test_coverage_gate_below_minimum():
    runner = FakeCommandRunner().add_rule(
        "cov", CommandResult(["cov"], 0, "TOTAL 82.5%", "")
    )
    result = CoverageGate("coverage", ["cov"], minimum=90.0).evaluate(_ctx(runner))
    assert result.passed is False
    assert "82.5%" in result.detail


def test_coverage_gate_command_failed():
    runner = FakeCommandRunner().add_rule("cov", CommandResult(["cov"], 1, "", "err"))
    result = CoverageGate("coverage", ["cov"]).evaluate(_ctx(runner))
    assert result.passed is False
    assert "command failed" in result.detail


def test_coverage_gate_no_percentage():
    runner = FakeCommandRunner().add_rule("cov", CommandResult(["cov"], 0, "no number", ""))
    result = CoverageGate("coverage", ["cov"]).evaluate(_ctx(runner))
    assert result.passed is False
    assert "no coverage" in result.detail


def test_predicate_gate():
    runner = FakeCommandRunner()
    ctx = GateContext(runner=runner)
    yes = PredicateGate("has-runner", lambda c: c.runner is not None, "ok")
    no = PredicateGate("impossible", lambda c: False)
    assert yes.evaluate(ctx).passed is True
    assert no.evaluate(ctx).passed is False


def test_definition_of_done_all_pass():
    runner = FakeCommandRunner()  # everything exits 0
    dod = DefinitionOfDone().add(CommandGate("a", ["x"])).add(CommandGate("b", ["y"]))
    report = dod.evaluate(_ctx(runner))
    assert report.passed is True
    assert report.failed_gates == []
    assert report.summary() == "2/2 gates passed"


def test_definition_of_done_some_fail():
    runner = FakeCommandRunner().add_rule("fail", CommandResult(["fail"], 1, "", "no"))
    dod = DefinitionOfDone([CommandGate("ok", ["pass"]), CommandGate("bad", ["fail"])])
    report = dod.evaluate(_ctx(runner))
    assert report.passed is False
    assert [g.name for g in report.failed_gates] == ["bad"]
    assert report.summary() == "1/2 gates passed"


def test_definition_of_done_empty_is_not_passed():
    report = DefinitionOfDone().evaluate(_ctx(FakeCommandRunner()))
    assert report.passed is False


def test_command_gate_over_dry_run_is_marked_not_executed():
    # A dry run exits 0, so .passed stays True (the engine's "nothing blocked"
    # contract is unchanged), but the result must be flagged not-executed.
    result = CommandGate("tests", ["pytest"]).evaluate(_ctx(DryRunCommandRunner()))
    assert result.passed is True
    assert result.executed is False
    assert "not executed" in result.detail


def test_summary_flags_dry_run_gates_as_not_executed():
    dod = DefinitionOfDone().add(CommandGate("a", ["x"])).add(CommandGate("b", ["y"]))
    report = dod.evaluate(_ctx(DryRunCommandRunner()))
    assert report.passed is True
    summary = report.summary()
    # Passing count is still reported, but the dry run is called out so the
    # line cannot read as real verification.
    assert "2/2 gates passed" in summary
    assert "dry-run: not executed" in summary


def test_coverage_gate_prefers_total_line():
    # A stray percentage after the TOTAL row must not win.
    output = "TOTAL    120   6   95%\nwarning: 3% of runs were slow"
    runner = FakeCommandRunner().add_rule("cov", CommandResult(["cov"], 0, output, ""))
    gate = CoverageGate("coverage", ["cov"], minimum=90.0)
    result = gate.evaluate(GateContext(runner=runner))
    assert result.passed is True
    assert "95.0%" in result.detail


def test_coverage_gate_total_line_without_percent_falls_back():
    output = "TOTAL row pending\ncoverage: 88%"
    runner = FakeCommandRunner().add_rule("cov", CommandResult(["cov"], 0, output, ""))
    gate = CoverageGate("coverage", ["cov"], minimum=90.0)
    result = gate.evaluate(GateContext(runner=runner))
    assert result.passed is False
    assert "88.0%" in result.detail


def test_coverage_gate_ignores_trailing_unrelated_percent():
    # No coverage-summary line at all — a bare trailing percentage must not be
    # mistaken for coverage; the gate fails closed instead of reading "50%".
    output = "3 tests passed\nnote: retried 50% of flaky steps"
    runner = FakeCommandRunner().add_rule("cov", CommandResult(["cov"], 0, output, ""))
    result = CoverageGate("coverage", ["cov"]).evaluate(GateContext(runner=runner))
    assert result.passed is False
    assert "no coverage percentage found" in result.detail


class _SequenceRunner:
    """Returns queued results in order; the last one repeats."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def run(self, command, *, cwd=None, timeout=None):
        self.calls.append(list(command))
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


def test_remote_ci_gate_passes_on_first_green_status():
    runner = FakeCommandRunner().add_rule(
        "ci status", CommandResult(["ci", "status"], 0, "run 42 succeeded", "")
    )
    slept = []
    gate = RemoteCIGate("remote-ci", ["ci", "status"], sleep=slept.append)
    result = gate.evaluate(GateContext(runner=runner))
    assert result.passed is True
    assert "run 42 succeeded" in result.detail
    assert slept == []


def test_remote_ci_gate_triggers_then_polls():
    runner = _SequenceRunner(
        CommandResult(["ci", "run"], 0, "queued", ""),
        CommandResult(["ci", "status"], 1, "in progress", ""),
        CommandResult(["ci", "status"], 0, "succeeded", ""),
    )
    slept = []
    gate = RemoteCIGate(
        "remote-ci",
        ["ci", "status"],
        trigger_command=["ci", "run"],
        poll_interval_seconds=5.0,
        sleep=slept.append,
    )
    result = gate.evaluate(GateContext(runner=runner))
    assert result.passed is True
    assert runner.calls == [["ci", "run"], ["ci", "status"], ["ci", "status"]]
    assert slept == [5.0]


def test_remote_ci_gate_fails_when_trigger_fails():
    runner = FakeCommandRunner().add_rule(
        "ci run", CommandResult(["ci", "run"], 1, "", "no such pipeline")
    )
    gate = RemoteCIGate("remote-ci", ["ci", "status"], trigger_command=["ci", "run"])
    result = gate.evaluate(GateContext(runner=runner))
    assert result.passed is False
    assert "remote trigger failed" in result.detail


def test_remote_ci_gate_fails_when_polls_exhausted():
    runner = FakeCommandRunner(default_exit_code=1).add_rule(
        "ci status", CommandResult(["ci", "status"], 1, "still failing", "")
    )
    gate = RemoteCIGate(
        "remote-ci", ["ci", "status"], max_polls=3, sleep=lambda _s: None
    )
    result = gate.evaluate(GateContext(runner=runner))
    assert result.passed is False
    assert "did not pass within 3 poll(s)" in result.detail
    assert "still failing" in result.detail
