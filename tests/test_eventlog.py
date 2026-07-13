"""Tests for the workspace event journal."""

from __future__ import annotations

import json
import threading
import time

import dev_team.eventlog as eventlog
from dev_team.eventlog import EVENTS_PATH, EventLog, compose, read_events
from dev_team.events import AgentEvent
from dev_team.execution import InMemoryWorkspace


def _event(role="engineer", stage="implement", message="working"):
    return AgentEvent(role=role, stage=stage, message=message, detail="d", name="Sam")


def test_event_log_appends_timestamped_records():
    ws = InMemoryWorkspace()
    ticks = iter([100.0, 101.5])
    log = EventLog(ws, run="deliver-1", clock=lambda: next(ticks))
    log(_event(message="first"))
    log(_event(role="qa", stage="test", message="second"))
    records = [json.loads(line) for line in ws.read_text(EVENTS_PATH).splitlines()]
    assert records[0] == {
        "ts": 100.0, "run": "deliver-1", "role": "engineer",
        "stage": "implement", "message": "first", "detail": "d", "name": "Sam",
    }
    assert records[1]["ts"] == 101.5
    assert records[1]["role"] == "qa"


def test_event_log_rotates_past_the_cap(monkeypatch):
    monkeypatch.setattr(eventlog, "MAX_EVENTS", 4)
    ws = InMemoryWorkspace()
    log = EventLog(ws, run="r", clock=lambda: 1.0)
    for i in range(6):
        log(_event(message=f"m{i}"))
    lines = ws.read_text(EVENTS_PATH).splitlines()
    # the 5th append tripped the cap (keep newest 2), the 6th appended onto that
    assert [json.loads(line)["message"] for line in lines] == ["m3", "m4", "m5"]


def test_read_events_returns_newest_and_skips_junk():
    ws = InMemoryWorkspace()
    log = EventLog(ws, run="r", clock=lambda: 1.0)
    for i in range(5):
        log(_event(message=f"m{i}"))
    text = ws.read_text(EVENTS_PATH)
    ws.write_text(EVENTS_PATH, 'not json\n"a string"\n' + text)
    events = read_events(ws, limit=3)
    assert [e["message"] for e in events] == ["m2", "m3", "m4"]


def test_read_events_handles_absent_and_unreadable_logs():
    assert read_events(InMemoryWorkspace()) == []

    class ExplodingWorkspace(InMemoryWorkspace):
        def read_text(self, path):
            raise OSError("disk gone")

    ws = ExplodingWorkspace({EVENTS_PATH: "{}"})
    assert read_events(ws) == []


def test_event_log_append_is_thread_safe_under_concurrency():
    # The append is read-modify-write; concurrent delivery agents journalling
    # at once would lose events (last writer wins) without the instance lock.
    # A workspace that yields the GIL between the read and the write widens
    # that race window: unlocked, most of the 12 events would be clobbered;
    # locked, every one survives because the whole RMW is serialised.
    class _SlowWorkspace(InMemoryWorkspace):
        def read_text(self, path):
            text = super().read_text(path)
            time.sleep(0.002)  # hand off to another delivery thread mid-RMW
            return text

    ws = _SlowWorkspace()
    log = EventLog(ws, run="r", clock=lambda: 1.0)
    threads = [
        threading.Thread(target=lambda i=i: log(_event(message=f"m{i}")))
        for i in range(12)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = [line for line in ws.read_text(EVENTS_PATH).splitlines() if line.strip()]
    messages = sorted(json.loads(line)["message"] for line in lines)
    assert messages == sorted(f"m{i}" for i in range(12))


def test_compose_listener_fan_out():
    assert compose(None, None) is None

    def lone(event):
        raise AssertionError("never called")

    assert compose(lone, None) is lone  # no wrapper when one listener

    first, second = [], []
    fan = compose(first.append, None, second.append)
    event = _event()
    fan(event)
    assert first == [event] and second == [event]
