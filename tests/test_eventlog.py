"""Tests for the workspace event journal."""

from __future__ import annotations

import json
import threading
import time

import dev_team.eventlog as eventlog
from dev_team.eventlog import EVENTS_PATH, EventLog, compose, read_events, remove_run
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


def test_remove_run_drops_matching_lines_keeps_others_in_order():
    ws = InMemoryWorkspace()
    lines = [
        json.dumps({"run": "A", "message": "a1"}),
        json.dumps({"run": "B", "message": "b1"}),
        "not json at all",
        json.dumps(["array", "not", "a", "dict"]),
        json.dumps({"run": "A", "message": "a2"}),
        json.dumps({"run": "C", "message": "c1"}),
        json.dumps({"run": "B", "message": "b2"}),
    ]
    ws.write_text(EVENTS_PATH, "\n".join(lines) + "\n")

    removed = remove_run(ws, "B")

    assert removed == 2
    # every non-"B" line survives, in its original relative order --
    # including the malformed line and the bare-JSON-array line, neither of
    # which can be attributed to run "B" so neither is dropped.
    assert ws.read_text(EVENTS_PATH).splitlines() == [
        lines[0], lines[2], lines[3], lines[4], lines[5],
    ]


def test_remove_run_missing_file_is_a_noop():
    ws = InMemoryWorkspace()
    assert remove_run(ws, "anything") == 0
    assert not ws.exists(EVENTS_PATH)


def test_remove_run_no_match_still_rewrites_unchanged_content():
    ws = InMemoryWorkspace()
    original = (
        json.dumps({"run": "A", "message": "a1"}) + "\n"
        + json.dumps({"run": "B", "message": "b1"}) + "\n"
    )
    ws.write_text(EVENTS_PATH, original)
    writes = []
    real_write_text = ws.write_text

    def spy_write_text(path, content):
        writes.append(content)
        real_write_text(path, content)

    ws.write_text = spy_write_text

    removed = remove_run(ws, "nonexistent-run")

    assert removed == 0
    # rewritten, not skipped -- byte-for-byte equal to the input, matching
    # EventLog's own always-rewrite behaviour.
    assert writes == [original]
    assert ws.read_text(EVENTS_PATH) == original


def test_remove_run_traversal_shaped_run_id_matches_nothing():
    # SECURITY: `run` is only ever compared against the parsed "run" field,
    # never used to build a path, so a traversal-shaped id just fails to
    # match -- it never escapes EVENTS_PATH.
    ws = InMemoryWorkspace()
    lines = [json.dumps({"run": "real-job", "message": "x"})]
    ws.write_text(EVENTS_PATH, "\n".join(lines) + "\n")

    for bad_id in ("../../etc/passwd", "a/../b", "../real-job", "real-job/.."):
        assert remove_run(ws, bad_id) == 0

    assert ws.read_text(EVENTS_PATH).splitlines() == lines


def test_remove_run_shares_a_lock_with_a_concurrent_event_log_append():
    # A shared lock must serialise EventLog.__call__'s append against
    # remove_run's read-modify-write of the same file -- without it, one
    # writer's read-modify-write can clobber the other's (a lost update).
    class _SlowWorkspace(InMemoryWorkspace):
        def read_text(self, path):
            text = super().read_text(path)
            time.sleep(0.002)  # hand off to the other mutator mid-RMW
            return text

    ws = _SlowWorkspace()
    ws.write_text(
        EVENTS_PATH,
        "\n".join(json.dumps({"run": "drop", "message": f"d{i}"}) for i in range(5)) + "\n",
    )
    shared_lock = threading.Lock()
    log = EventLog(ws, run="keep", clock=lambda: 1.0, lock=shared_lock)

    append_threads = [
        threading.Thread(target=lambda i=i: log(_event(message=f"k{i}")))
        for i in range(8)
    ]
    remove_threads = [
        threading.Thread(
            target=lambda: remove_run(ws, "drop", lock=shared_lock)
        )
        for _ in range(3)
    ]
    threads = append_threads + remove_threads
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    remaining = [json.loads(line) for line in ws.read_text(EVENTS_PATH).splitlines() if line.strip()]
    # every "keep" append survived (no lost update), and every "drop" line
    # was removed and never reappears, regardless of interleaving order.
    assert sorted(e["message"] for e in remaining if e["run"] == "keep") == sorted(
        f"k{i}" for i in range(8)
    )
    assert [e for e in remaining if e["run"] == "drop"] == []


def test_event_log_default_lock_is_independent_per_instance():
    # Regression for the new optional `lock` parameter: omitting it must
    # still behave exactly as before -- each EventLog gets its own private
    # lock, so two independently-constructed logs never block each other.
    ws = InMemoryWorkspace()
    first = EventLog(ws, run="a", clock=lambda: 1.0)
    second = EventLog(ws, run="b", clock=lambda: 2.0)
    assert first._lock is not second._lock
    first(_event(message="from-a"))
    second(_event(message="from-b"))
    messages = [json.loads(line)["message"] for line in ws.read_text(EVENTS_PATH).splitlines()]
    assert messages == ["from-a", "from-b"]


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
