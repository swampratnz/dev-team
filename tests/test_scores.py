"""Tests for the persisted score trail."""

from __future__ import annotations

from dev_team.execution import InMemoryWorkspace
from dev_team.scores import _MAX_SCORE_RUNS, RunScore, ScoreHistory


def _score(feature="F", *, success=True, attempts=1, cost=0.0, scorecard=None):
    return RunScore(
        feature=feature,
        success=success,
        tasks_total=1,
        tasks_succeeded=1 if success else 0,
        total_attempts=attempts,
        cost_usd=cost,
        committed=success,
        scorecard=scorecard or {},
    )


def test_record_and_load_roundtrip():
    ws = InMemoryWorkspace()
    history = ScoreHistory(ws)
    history.record(_score("First", cost=0.01))
    history.record(_score("Second", cost=0.02))
    loaded = history.load()
    assert [s.feature for s in loaded] == ["First", "Second"]
    assert loaded[1].cost_usd == 0.02
    assert loaded[0].scorecard == {}


def test_load_absent_is_empty():
    assert ScoreHistory(InMemoryWorkspace()).load() == []


def test_load_corrupt_or_non_list_is_empty():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    ws.write_text(hist.path, "not json{")
    assert hist.load() == []
    ws.write_text(hist.path, '{"not": "a list"}')
    assert hist.load() == []


def test_load_skips_non_dict_entries():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    ws.write_text(hist.path, '[42, {"feature": "ok"}]')
    loaded = hist.load()
    assert [s.feature for s in loaded] == ["ok"]


def test_run_score_from_dict_defaults_and_bad_scorecard():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    # a partial record (older format) and a non-dict scorecard both survive
    ws.write_text(hist.path, '[{"feature": "F"}, {"feature": "G", "scorecard": 7}]')
    loaded = hist.load()
    assert loaded[0].tasks_total == 0 and loaded[0].scorecard == {}
    assert loaded[1].scorecard == {}


def test_record_is_bounded():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    for i in range(_MAX_SCORE_RUNS + 5):
        hist.record(_score(f"run-{i}"))
    loaded = hist.load()
    assert len(loaded) == _MAX_SCORE_RUNS
    # the oldest runs were dropped; the newest are kept
    assert loaded[-1].feature == f"run-{_MAX_SCORE_RUNS + 4}"


def test_render_empty():
    assert ScoreHistory(InMemoryWorkspace()).render() == "No delivery runs recorded yet."


def test_render_shows_headline_and_deltas():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    hist.record(
        _score("First", attempts=2, cost=0.05, scorecard={"gate_failures": 1, "review_rejections": 2})
    )
    # increases (+ deltas) and an unchanged scorecard key (review_rejections)
    hist.record(
        _score(
            "Second", success=False, attempts=5, cost=0.02,
            scorecard={"gate_failures": 3, "review_rejections": 2},
        )
    )
    # decreases (- deltas) and an unchanged cost
    hist.record(
        _score(
            "Third", attempts=1, cost=0.02,
            scorecard={"gate_failures": 0, "review_rejections": 2},
        )
    )
    out = hist.render()
    lines = out.splitlines()
    assert "3 run(s), newest last" in out
    assert "- First: ok, 1/1 tasks, 2 attempt(s), $0.0500" in lines[1]
    assert "| delta" not in lines[1]  # first run has no prior to diff against
    # Second vs First: signed increases, cost drop, unchanged key omitted
    assert "attempts +3" in lines[2] and "cost -$0.0300" in lines[2]
    assert "gate_failures +2" in lines[2] and "review_rejections" not in lines[2]
    # Third vs Second: signed decreases, cost unchanged (omitted)
    assert "attempts -4" in lines[3] and "gate_failures -3" in lines[3]
    assert "cost" not in lines[3]


def test_render_no_delta_annotation_when_metrics_unchanged():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    hist.record(_score("A", attempts=1, cost=0.01))
    hist.record(_score("B", attempts=1, cost=0.01))
    lines = hist.render().splitlines()
    assert lines[-1] == "- B: ok, 1/1 tasks, 1 attempt(s), $0.0100"  # no "| delta"


def test_score_deltas_pick_up_design_thoroughness_keys_generically():
    # Regression test: the generic key-union mechanism in `_score_deltas`
    # picks up new scorecard keys (design_components_count etc., issue #178)
    # with no production changes to this module.
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    hist.record(
        _score(
            "First",
            scorecard={
                "design_components_count": 1,
                "design_risks_count": 1,
                "design_alternatives_count": 0,
            },
        )
    )
    hist.record(
        _score(
            "Second",
            scorecard={
                "design_components_count": 3,
                "design_risks_count": 0,
                "design_alternatives_count": 2,
            },
        )
    )
    delta = hist.latest_delta()
    assert delta is not None
    assert "design_components_count +2" in delta
    assert "design_risks_count -1" in delta
    assert "design_alternatives_count +2" in delta


def test_latest_delta_edges():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    assert hist.latest_delta() is None  # nothing recorded
    hist.record(_score("A", cost=0.01))
    assert hist.latest_delta() is None  # only one run
    hist.record(_score("B", cost=0.01))
    assert hist.latest_delta() is None  # unchanged metrics
    hist.record(_score("C", cost=0.05, attempts=3, scorecard={"gate_failures": 2}))
    delta = hist.latest_delta()
    assert delta is not None
    assert "cost +$0.0400" in delta and "attempts +2" in delta and "gate_failures +2" in delta
