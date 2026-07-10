"""Tests for the instrumented runner (budget + trace)."""

from __future__ import annotations

from helpers import run

from dev_team.budget import Budget
from dev_team.instrument import InstrumentedRunner
from dev_team.sdk import AgentResult
from dev_team.testing import ScriptedRunner
from dev_team.trace import Tracer


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def test_records_budget_and_trace():
    inner = ScriptedRunner([AgentResult(text="hi", cost_usd=0.5, num_turns=2)])
    budget = Budget()
    tracer = Tracer(clock=_Clock())
    runner = InstrumentedRunner(inner, "engineer", budget=budget, tracer=tracer)
    result = run(runner.run("prompt", system_prompt="sys"))
    assert result.text == "hi"
    assert budget.spent == 0.5
    assert tracer.spans[0].kind == "agent"
    assert tracer.spans[0].name == "engineer"
    assert tracer.spans[0].status == "ok"


def test_error_result_marks_span_error():
    inner = ScriptedRunner([AgentResult(text="", is_error=True)])
    tracer = Tracer(clock=_Clock())
    runner = InstrumentedRunner(inner, "qa", tracer=tracer)
    run(runner.run("p"))
    assert tracer.spans[0].status == "error"


def test_works_without_budget_or_tracer():
    inner = ScriptedRunner([AgentResult(text="ok")])
    runner = InstrumentedRunner(inner, "pm")
    result = run(runner.run("p", model="m", allowed_tools=["Read"]))
    assert result.text == "ok"
