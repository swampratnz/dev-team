"""Tests for the durable trace journal (Tracer's sink)."""

from __future__ import annotations

import json

import dev_team.tracelog as tracelog
from dev_team.execution import InMemoryWorkspace
from dev_team.trace import Tracer
from dev_team.tracelog import TRACE_PATH, TraceLog, read_trace_log


def test_trace_log_appends_one_line_with_exactly_the_documented_keys():
    ws = InMemoryWorkspace()
    ticks = iter([100.0, 101.5])
    log = TraceLog(ws, run="deliver-1", clock=lambda: next(ticks))
    tracer = Tracer(clock=lambda: 1.0, sink=log)
    tracer.end(tracer.start("agent", "engineer", cost_usd="0.5"), "ok")
    lines = ws.read_text(TRACE_PATH).splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record.keys()) == {
        "ts", "run", "seq", "kind", "name", "status", "duration", "attributes",
    }
    assert record["ts"] == 100.0
    assert record["run"] == "deliver-1"
    assert record["seq"] == 0
    assert record["kind"] == "agent"
    assert record["name"] == "engineer"
    assert record["status"] == "ok"
    assert record["duration"] == 0.0
    assert record["attributes"] == {"cost_usd": "0.5"}


def test_trace_log_rotates_past_the_cap(monkeypatch):
    monkeypatch.setattr(tracelog, "MAX_TRACE_SPANS", 4)
    ws = InMemoryWorkspace()
    log = TraceLog(ws, run="r", clock=lambda: 1.0)
    tracer = Tracer(clock=lambda: 1.0, sink=log)
    for i in range(6):
        tracer.end(tracer.start("agent", f"a{i}"))
    lines = ws.read_text(TRACE_PATH).splitlines()
    # the 5th append tripped the cap (keep newest 2), the 6th appended onto that
    assert [json.loads(line)["name"] for line in lines] == ["a3", "a4", "a5"]


def test_read_trace_log_returns_newest_and_skips_junk():
    ws = InMemoryWorkspace()
    log = TraceLog(ws, run="r", clock=lambda: 1.0)
    tracer = Tracer(clock=lambda: 1.0, sink=log)
    for i in range(5):
        tracer.end(tracer.start("agent", f"a{i}"))
    text = ws.read_text(TRACE_PATH)
    ws.write_text(TRACE_PATH, 'not json\n"a string"\n' + text)
    spans = read_trace_log(ws, limit=3)
    assert [s["name"] for s in spans] == ["a2", "a3", "a4"]


def test_read_trace_log_handles_absent_and_unreadable_logs():
    assert read_trace_log(InMemoryWorkspace()) == []

    class ExplodingWorkspace(InMemoryWorkspace):
        def read_text(self, path):
            raise OSError("disk gone")

    ws = ExplodingWorkspace({TRACE_PATH: "{}"})
    assert read_trace_log(ws) == []


def test_write_failure_is_swallowed_and_never_raises_out_of_tracer_end():
    class BoomWorkspace(InMemoryWorkspace):
        def write_text(self, path, content):
            raise OSError("disk full")

    log = TraceLog(BoomWorkspace(), run="r", clock=lambda: 1.0)
    tracer = Tracer(clock=lambda: 1.0, sink=log)
    span = tracer.start("agent", "engineer")
    tracer.end(span)  # must not raise despite the workspace refusing to write
    assert span.status == "ok"


def test_never_persists_prompt_or_response_text():
    # TraceSpan has never carried prompt/response text, so a marker planted in
    # an attribute (simulating repository content leaking into a span, the
    # only route by which text could reach the sink) must never appear in the
    # persisted file — pinning "metadata only" as an enforced contract.
    marker = "super-secret-prompt-marker-4f8c"
    ws = InMemoryWorkspace()
    log = TraceLog(ws, run="r", clock=lambda: 1.0)
    tracer = Tracer(clock=lambda: 1.0, sink=log)
    span = tracer.start("agent", "engineer", prompt_hint=marker)
    tracer.end(span)
    raw = ws.read_text(TRACE_PATH)
    assert marker in raw  # attributes ARE persisted...
    record = json.loads(raw.splitlines()[0])
    assert set(record.keys()) == {
        "ts", "run", "seq", "kind", "name", "status", "duration", "attributes",
    }
    assert "system_prompt" not in record
    assert "prompt" not in record
    assert "response" not in record
