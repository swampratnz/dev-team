"""Tests for executable quality gates and the Definition of Done."""

from __future__ import annotations

from dev_team.execution import CommandResult, FakeCommandRunner
from dev_team.verification import (
    CommandGate,
    CoverageGate,
    DefinitionOfDone,
    GateContext,
    PredicateGate,
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
