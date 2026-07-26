"""Tests for the dispatch/dashboard failed-auth rate limiter (issue #214)."""

from __future__ import annotations

import threading

from dev_team.authguard import (
    DEFAULT_LOCKOUT_SECONDS,
    DEFAULT_MAX_TRACKED_SOURCES,
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW_SECONDS,
    FailedAuthTracker,
)


def _fake_clock(start: float = 0.0):
    """A mutable, manually-advanced clock: ``box[0] += n`` moves time forward."""

    box = [start]
    return box, (lambda: box[0])


def test_defaults_match_the_documented_values():
    assert DEFAULT_THRESHOLD == 10
    assert DEFAULT_WINDOW_SECONDS == 60.0
    assert DEFAULT_LOCKOUT_SECONDS == 60.0
    assert DEFAULT_MAX_TRACKED_SOURCES == 4096


# --- criterion 1: below threshold, no lockout (regression) -----------------


def test_requests_below_threshold_never_lock_out():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=5, window_seconds=60, lockout_seconds=60, clock=clock)
    for _ in range(4):  # threshold - 1
        tracker.record_failure("1.2.3.4")
        assert tracker.is_locked_out("1.2.3.4") is None


# --- criterion 2: the threshold-th failure trips; only the NEXT is gated ---


def test_threshold_th_failure_still_not_locked_but_next_request_is():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=3, window_seconds=60, lockout_seconds=60, clock=clock)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    # The 3rd (threshold-th) failure trips the counter, but the request that
    # caused it is itself evaluated normally — is_locked_out is checked
    # BEFORE record_failure by callers, so it must still read "not locked"
    # right up until record_failure appends the tripping entry.
    assert tracker.is_locked_out("1.2.3.4") is None
    tracker.record_failure("1.2.3.4")
    remaining = tracker.is_locked_out("1.2.3.4")
    assert remaining is not None
    assert 0 < remaining <= 60


def test_lockout_retry_after_counts_down_from_the_tripping_failure():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=2, window_seconds=60, lockout_seconds=60, clock=clock)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked_out("1.2.3.4") == 60
    box[0] += 25
    remaining = tracker.is_locked_out("1.2.3.4")
    assert remaining == 35


# --- criterion 3: a correct token clears history; a later miss is fresh ----


def test_success_clears_history_so_a_later_miss_starts_fresh():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=3, window_seconds=60, lockout_seconds=60, clock=clock)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    tracker.record_success("1.2.3.4")
    # One more failure after the reset is only the first of a fresh count,
    # nowhere near the threshold.
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked_out("1.2.3.4") is None


def test_record_success_on_an_untracked_key_is_a_no_op():
    _, clock = _fake_clock()
    tracker = FailedAuthTracker(clock=clock)
    tracker.record_success("never-seen")  # must not raise
    assert len(tracker) == 0


# --- criterion 4: lockout expiry, injected clock, no real sleep ------------


def test_lockout_expires_after_lockout_seconds_elapse():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=2, window_seconds=120, lockout_seconds=10, clock=clock)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked_out("1.2.3.4") is not None
    box[0] += 9.999
    assert tracker.is_locked_out("1.2.3.4") is not None
    box[0] += 1  # now 10.999 seconds past the tripping failure
    assert tracker.is_locked_out("1.2.3.4") is None


# --- criterion 5: two sources tracked independently -------------------------


def test_two_sources_are_tracked_independently():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=2, window_seconds=60, lockout_seconds=60, clock=clock)
    tracker.record_failure("1.1.1.1")
    tracker.record_failure("1.1.1.1")
    assert tracker.is_locked_out("1.1.1.1") is not None
    assert tracker.is_locked_out("2.2.2.2") is None
    # The unaffected source's own successes/failures are unaffected too.
    tracker.record_failure("2.2.2.2")
    tracker.record_success("2.2.2.2")
    assert tracker.is_locked_out("2.2.2.2") is None


# --- criterion 6: SECURITY — never stores the attempted token --------------


def test_never_stores_a_token_value_or_derivative():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=100, clock=clock)
    attempted_tokens = ["s3cr3t-one", "s3cr3t-two", "s3cr3t-three-longer-value"]
    for tok in attempted_tokens:
        # The tracker's public API never even accepts a token — record_failure
        # only ever takes the source key. Simulate the call sites' behaviour:
        # only the source IP crosses into the tracker, never `tok` itself.
        tracker.record_failure("9.9.9.9")
        assert tok not in repr(tracker._failures)
    for tok in attempted_tokens:
        assert tok not in repr(tracker._failures)
        for timestamps in tracker._failures.values():
            for entry in timestamps:
                assert not isinstance(entry, str)


# --- criterion 7: SECURITY / bounded memory ---------------------------------


def test_bounded_memory_evicts_the_oldest_source_past_the_cap():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=1, max_tracked_sources=3, clock=clock)
    tracker.record_failure("a")
    tracker.record_failure("b")
    tracker.record_failure("c")
    assert len(tracker) == 3
    tracker.record_failure("d")
    assert len(tracker) == 3
    assert list(tracker._failures.keys()) == ["b", "c", "d"]


def test_bounded_memory_never_exceeds_cap_under_a_sustained_flood():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=1, max_tracked_sources=50, clock=clock)
    for i in range(500):
        tracker.record_failure(f"10.0.0.{i}")
        assert len(tracker) <= 50
    assert len(tracker) == 50


# --- criterion 8: concurrency — no lost updates -----------------------------


def test_concurrent_failures_from_one_source_are_all_recorded():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=10_000, window_seconds=60, clock=clock)
    n = 200
    threads = [
        threading.Thread(target=lambda: tracker.record_failure("shared-ip"))
        for _ in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(tracker._failures["shared-ip"]) == n


# --- criterion 10 / disable spelling ----------------------------------------


def test_threshold_zero_disables_the_guard_entirely():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=0, clock=clock)
    for _ in range(50):
        tracker.record_failure("1.2.3.4")
        assert tracker.is_locked_out("1.2.3.4") is None
    assert len(tracker) == 0  # disabled: nothing is even tracked


def test_negative_threshold_also_disables_the_guard():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=-1, clock=clock)
    tracker.record_failure("1.2.3.4")
    assert tracker.is_locked_out("1.2.3.4") is None


# --- misc: pruning / default clock ------------------------------------------


def test_stale_failures_outside_the_window_are_pruned_and_do_not_lock_out():
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=3, window_seconds=10, lockout_seconds=60, clock=clock)
    tracker.record_failure("1.2.3.4")
    tracker.record_failure("1.2.3.4")
    box[0] += 11  # both failures now outside the 10s window
    tracker.record_failure("1.2.3.4")
    # Only 1 failure remains within the window — nowhere near threshold 3.
    assert tracker.is_locked_out("1.2.3.4") is None


def test_is_locked_out_prunes_a_fully_stale_entry_and_forgets_the_key():
    # Unlike test_stale_failures_..._are_pruned above (which prunes via a
    # later record_failure call), this exercises is_locked_out's OWN prune
    # path emptying the deque with no further record_failure call at all —
    # the key must be forgotten (not just left as an empty deque) so the
    # tracker's bounded-memory accounting stays accurate.
    box, clock = _fake_clock()
    tracker = FailedAuthTracker(threshold=1, window_seconds=5, lockout_seconds=60, clock=clock)
    tracker.record_failure("1.2.3.4")
    assert len(tracker) == 1
    box[0] += 10  # past the 5s window
    assert tracker.is_locked_out("1.2.3.4") is None
    assert len(tracker) == 0  # the stale key was forgotten, not just emptied


def test_is_locked_out_on_a_never_seen_key_is_none():
    _, clock = _fake_clock()
    tracker = FailedAuthTracker(clock=clock)
    assert tracker.is_locked_out("brand-new") is None


def test_default_clock_is_time_monotonic():
    import time

    tracker = FailedAuthTracker()
    assert tracker.clock is time.monotonic
