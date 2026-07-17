"""Tests for the persisted benchmark trend trail."""

from __future__ import annotations

import json

from dev_team.benchmark_history import (
    _MAX_HISTORY_RUNS,
    BenchmarkHistory,
    BenchmarkRun,
    _benchmark_run_from_dict,
)


def _run(*, total=2, passed=2, cost=0.0, timestamp="2026-01-01T00:00:00+00:00"):
    return BenchmarkRun(
        cases_total=total, cases_passed=passed, cost_usd=cost, timestamp=timestamp
    )


def test_benchmark_run_roundtrips_through_json():
    run = _run(total=3, passed=2, cost=0.125, timestamp="2026-02-03T04:05:06+00:00")
    restored = _benchmark_run_from_dict(json.loads(json.dumps(vars(run))))
    assert restored == run


def test_from_dict_defaults_missing_fields():
    restored = _benchmark_run_from_dict({"cases_total": 5})
    assert restored == BenchmarkRun(
        cases_total=5, cases_passed=0, cost_usd=0.0, timestamp=""
    )
    assert _benchmark_run_from_dict({}) == BenchmarkRun(0, 0, 0.0, "")


def test_record_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "history.json")
    history = BenchmarkHistory(path)
    history.record(_run(total=2, passed=2, cost=0.01))
    history.record(_run(total=2, passed=1, cost=0.02))
    loaded = history.load()
    assert [r.cost_usd for r in loaded] == [0.01, 0.02]
    assert loaded[1].cases_passed == 1


def test_record_creates_parent_directories(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "history.json")
    history = BenchmarkHistory(path)
    history.record(_run())
    assert history.load() == [_run()]


def test_record_is_bounded(tmp_path):
    path = str(tmp_path / "history.json")
    history = BenchmarkHistory(path)
    for i in range(_MAX_HISTORY_RUNS + 5):
        history.record(_run(total=1, passed=1, cost=float(i)))
    loaded = history.load()
    assert len(loaded) == _MAX_HISTORY_RUNS
    # the oldest runs were dropped; the newest are kept
    assert loaded[-1].cost_usd == float(_MAX_HISTORY_RUNS + 4)


def test_load_missing_file_is_empty(tmp_path):
    history = BenchmarkHistory(str(tmp_path / "missing.json"))
    assert history.load() == []


def test_load_invalid_json_is_empty(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("not json{")
    assert BenchmarkHistory(str(path)).load() == []


def test_load_non_list_is_empty(tmp_path):
    path = tmp_path / "history.json"
    path.write_text('{"not": "a list"}')
    assert BenchmarkHistory(str(path)).load() == []


def test_load_skips_non_dict_entries(tmp_path):
    path = tmp_path / "history.json"
    path.write_text('[42, {"cases_total": 2, "cases_passed": 2}]')
    loaded = BenchmarkHistory(str(path)).load()
    assert len(loaded) == 1
    assert loaded[0].cases_total == 2


def test_latest_delta_none_with_fewer_than_two_runs(tmp_path):
    path = str(tmp_path / "history.json")
    history = BenchmarkHistory(path)
    assert history.latest_delta() is None
    history.record(_run(total=2, passed=2, cost=0.01))
    assert history.latest_delta() is None


def test_latest_delta_reports_signed_pass_rate_and_cost(tmp_path):
    path = str(tmp_path / "history.json")
    history = BenchmarkHistory(path)
    history.record(_run(total=2, passed=1, cost=0.05))
    history.record(_run(total=2, passed=2, cost=0.02))
    delta = history.latest_delta()
    assert delta is not None
    assert "pass-rate +50.0pp" in delta
    assert "cost -$0.0300" in delta


def test_latest_delta_negative_deltas(tmp_path):
    path = str(tmp_path / "history.json")
    history = BenchmarkHistory(path)
    history.record(_run(total=2, passed=2, cost=0.01))
    history.record(_run(total=2, passed=0, cost=0.05))
    delta = history.latest_delta()
    assert "pass-rate -100.0pp" in delta
    assert "cost +$0.0400" in delta


def test_latest_delta_handles_zero_total_cases(tmp_path):
    path = str(tmp_path / "history.json")
    history = BenchmarkHistory(path)
    history.record(_run(total=0, passed=0, cost=0.0))
    history.record(_run(total=0, passed=0, cost=0.0))
    delta = history.latest_delta()
    assert "pass-rate +0.0pp" in delta
    assert "cost +$0.0000" in delta
