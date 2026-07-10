"""Tests for the audit tracer."""

from __future__ import annotations

from dev_team.trace import Tracer


class FakeClock:
    """A deterministic, monotonically increasing clock."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def test_span_lifecycle_and_duration():
    tracer = Tracer(clock=FakeClock())
    span = tracer.start("agent", "engineer", attempt="1")
    assert span.duration is None  # still open
    tracer.end(span)
    assert span.status == "ok"
    assert span.duration == 1.0  # 2.0 - 1.0
    assert span.attributes == {"attempt": "1"}


def test_event_is_zero_ish_duration():
    tracer = Tracer(clock=FakeClock())
    span = tracer.event("tool", "pytest")
    assert span.duration == 1.0
    assert span.seq == 0


def test_by_kind_and_render():
    tracer = Tracer(clock=FakeClock())
    tracer.end(tracer.start("agent", "a"))
    tracer.end(tracer.start("tool", "b"), status="error")
    open_span = tracer.start("agent", "c")
    assert [s.name for s in tracer.by_kind("agent")] == ["a", "c"]
    rendered = tracer.render()
    assert "#0 [agent] a ok" in rendered
    assert "[tool] b error" in rendered
    # open span renders without a duration suffix
    assert "#2 [agent] c ok" in rendered
    assert open_span.ended_at is None


def test_default_clock_used_when_none():
    tracer = Tracer()
    span = tracer.event("x", "y")
    # Real clock returns a float; duration should be a non-negative number.
    assert span.duration is not None
    assert span.duration >= 0.0
