"""Tests for progress events."""

from __future__ import annotations

from dev_team.events import AgentEvent, emit


def test_event_str_without_detail():
    event = AgentEvent(role="qa", stage="testing", message="working")
    assert str(event) == "[qa/testing] working"


def test_event_str_with_detail():
    event = AgentEvent(role="qa", stage="testing", message="done", detail="2 turns")
    assert str(event) == "[qa/testing] done (2 turns)"


def test_emit_calls_listener():
    seen = []
    event = AgentEvent(role="x", stage="s", message="m")
    emit(seen.append, event)
    assert seen == [event]


def test_emit_with_none_listener_is_noop():
    # Should not raise.
    emit(None, AgentEvent(role="x", stage="s", message="m"))
