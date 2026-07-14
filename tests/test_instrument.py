"""Tests for the instrumented runner (budget + trace)."""

from __future__ import annotations

from helpers import run

from dev_team.budget import Budget, BudgetExceededError
from dev_team.execution import InMemoryWorkspace
from dev_team.instrument import InstrumentedRunner
from dev_team.sdk import AgentResult
from dev_team.testing import ScriptedRunner
from dev_team.trace import Tracer
from dev_team.transcripts import TranscriptRecorder, list_transcripts, read_transcript


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


def test_exception_closes_span_and_records_nothing():
    class BoomRunner:
        async def run(self, prompt, **kwargs):
            raise RuntimeError("boom")

    budget = Budget()
    tracer = Tracer(clock=_Clock())
    runner = InstrumentedRunner(BoomRunner(), "engineer", budget=budget, tracer=tracer)
    try:
        run(runner.run("p"))
    except RuntimeError:
        pass
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("expected RuntimeError")
    assert tracer.spans[0].status == "exception"
    assert budget.meter.call_count == 0


def test_exception_without_tracer_still_propagates():
    class BoomRunner:
        async def run(self, prompt, **kwargs):
            raise RuntimeError("boom")

    runner = InstrumentedRunner(BoomRunner(), "engineer")
    try:
        run(runner.run("p"))
    except RuntimeError:
        pass
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("expected RuntimeError")


# --- transcript recording ----------------------------------------------------


def test_records_transcript_on_success():
    inner = ScriptedRunner([AgentResult(text="hello", cost_usd=0.4)])
    ws = InMemoryWorkspace()
    recorder = TranscriptRecorder(ws, run="deliver-1", clock=lambda: 7.0)
    runner = InstrumentedRunner(inner, "engineer", transcript_recorder=recorder)
    run(runner.run("do it", system_prompt="be an engineer"))
    record = read_transcript(ws, "deliver-1", "engineer", 1)
    assert record["prompt"] == "do it"
    assert record["system_prompt"] == "be an engineer"
    assert record["response"] == "hello"
    assert record["cost_usd"] == 0.4


def test_records_transcript_on_error_result():
    # An error RESULT (not a raise) is still recorded so failures are auditable.
    inner = ScriptedRunner([AgentResult(text="boom", is_error=True)])
    ws = InMemoryWorkspace()
    recorder = TranscriptRecorder(ws, run="deliver-1")
    runner = InstrumentedRunner(inner, "qa", transcript_recorder=recorder)
    run(runner.run("p"))
    record = read_transcript(ws, "deliver-1", "qa", 1)
    assert record["is_error"] is True


def test_does_not_record_on_raising_call():
    class BoomRunner:
        async def run(self, prompt, **kwargs):
            raise RuntimeError("boom")

    ws = InMemoryWorkspace()
    recorder = TranscriptRecorder(ws, run="deliver-1")
    runner = InstrumentedRunner(BoomRunner(), "engineer", transcript_recorder=recorder)
    try:
        run(runner.run("p"))
    except RuntimeError:
        pass
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("expected RuntimeError")
    # no result to record, so nothing was written
    assert list_transcripts(ws, "deliver-1", "engineer") == []


def test_transcript_written_before_budget_enforcement():
    # The call whose cost tips the budget over its ceiling has still been paid
    # for; its transcript must be written BEFORE budget.record raises, or the
    # paid I/O is lost to the exception and never audited.
    inner = ScriptedRunner([AgentResult(text="pricey", cost_usd=2.0, num_turns=1)])
    ws = InMemoryWorkspace()
    recorder = TranscriptRecorder(ws, run="deliver-1")
    budget = Budget(limit_usd=1.0)
    runner = InstrumentedRunner(
        inner, "engineer", budget=budget, transcript_recorder=recorder
    )
    try:
        run(runner.run("do it", system_prompt="be an engineer"))
    except BudgetExceededError:
        pass
    else:  # pragma: no cover - the raise is the point
        raise AssertionError("expected BudgetExceededError")
    # the paid call's transcript survived the budget death
    record = read_transcript(ws, "deliver-1", "engineer", 1)
    assert record["response"] == "pricey"
    assert record["cost_usd"] == 2.0


def test_transcript_write_failure_never_breaks_the_run():
    class BoomRecorder:
        def record(self, **kwargs):
            raise OSError("disk full")

    inner = ScriptedRunner([AgentResult(text="ok")])
    runner = InstrumentedRunner(inner, "engineer", transcript_recorder=BoomRecorder())
    # a recording failure is swallowed; the call still returns its result
    result = run(runner.run("p"))
    assert result.text == "ok"
