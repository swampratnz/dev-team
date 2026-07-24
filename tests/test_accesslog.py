"""Tests for the dispatch service's bounded HTTP access-log journal."""

from __future__ import annotations

import json
import threading

import dev_team.accesslog as accesslog
from dev_team.accesslog import (
    ACCESS_LOG_FILENAME,
    AccessLog,
    MAX_PATH_BYTES,
    read_access_log,
)


def test_append_writes_a_timestamped_record(tmp_path):
    ticks = iter([100.0, 101.5])
    log = AccessLog(str(tmp_path), clock=lambda: next(ticks))
    log.append(method="GET", request_path="/health", status=200)
    log.append(method="POST", request_path="/jobs", status=202)
    lines = (tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()
    records = [json.loads(line) for line in lines]
    assert records[0] == {"ts": 100.0, "method": "GET", "path": "/health", "status": 200}
    assert records[1] == {"ts": 101.5, "method": "POST", "path": "/jobs", "status": 202}


def test_append_omits_job_id_when_not_given(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="GET", request_path="/jobs", status=200)
    record = json.loads((tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0])
    assert record == {"ts": 1.0, "method": "GET", "path": "/jobs", "status": 200}
    assert "job_id" not in record


def test_append_includes_job_id_when_given(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="POST", request_path="/jobs", status=202, job_id="deliver-1")
    record = json.loads((tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0])
    assert record == {
        "ts": 1.0,
        "method": "POST",
        "path": "/jobs",
        "status": 202,
        "job_id": "deliver-1",
    }


def test_read_access_log_round_trips_a_job_id_bearing_record(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="POST", request_path="/jobs", status=202, job_id="deliver-1")
    records = read_access_log(str(tmp_path))
    assert records == [
        {"ts": 1.0, "method": "POST", "path": "/jobs", "status": 202, "job_id": "deliver-1"}
    ]


def test_append_omits_job_ids_when_not_given(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="POST", request_path="/foreman/run", status=200)
    record = json.loads((tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0])
    assert "job_ids" not in record


def test_append_omits_job_ids_when_given_an_empty_list(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="POST", request_path="/foreman/run", status=200, job_ids=[])
    record = json.loads((tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0])
    assert "job_ids" not in record


def test_append_includes_job_ids_when_given(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(
        method="POST",
        request_path="/foreman/run",
        status=202,
        job_ids=["deliver-1", "deliver-2"],
    )
    record = json.loads((tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0])
    assert record == {
        "ts": 1.0,
        "method": "POST",
        "path": "/foreman/run",
        "status": 202,
        "job_ids": ["deliver-1", "deliver-2"],
    }


def test_append_never_sets_job_id_and_job_ids_together(tmp_path):
    # No real call site sets both, but the field-omission logic for each is
    # independent, so a hypothetical caller that passed both would still get
    # both persisted rather than one silently clobbering the other.
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(
        method="POST",
        request_path="/x",
        status=200,
        job_id="a",
        job_ids=["b"],
    )
    record = json.loads((tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0])
    assert record["job_id"] == "a"
    assert record["job_ids"] == ["b"]


def test_read_access_log_round_trips_a_job_ids_bearing_record(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(
        method="POST",
        request_path="/foreman/run",
        status=202,
        job_ids=["deliver-1", "deliver-2"],
    )
    records = read_access_log(str(tmp_path))
    assert records == [
        {
            "ts": 1.0,
            "method": "POST",
            "path": "/foreman/run",
            "status": 202,
            "job_ids": ["deliver-1", "deliver-2"],
        }
    ]


def test_append_creates_the_jobs_root_lazily(tmp_path):
    root = tmp_path / "jobs" / "nested"
    assert not root.exists()
    log = AccessLog(str(root), clock=lambda: 1.0)
    log.append(method="GET", request_path="/health", status=200)
    assert (root / ACCESS_LOG_FILENAME).exists()


def test_append_rotates_past_the_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(accesslog, "MAX_ACCESS_RECORDS", 4)
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    for i in range(6):
        log.append(method="GET", request_path=f"/p{i}", status=200)
    lines = (tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()
    # the 5th append tripped the cap (keep newest 2), the 6th appended onto that
    assert [json.loads(line)["path"] for line in lines] == ["/p3", "/p4", "/p5"]


def test_append_is_thread_safe_under_concurrency(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    threads = [
        threading.Thread(
            target=lambda i=i: log.append(method="GET", request_path=f"/p{i}", status=200)
        )
        for i in range(12)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()
    paths = sorted(json.loads(line)["path"] for line in lines)
    assert paths == sorted(f"/p{i}" for i in range(12))


def test_append_never_leaks_the_token_or_a_body_marker(tmp_path):
    # The append() contract only ever accepts method/path/status — there is
    # no parameter through which an Authorization header or a request body
    # could reach the persisted record. Simulate the auth-miss and the
    # POST-body dispatch scenarios and assert the sentinel values never
    # appear in the file, byte for byte.
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="GET", request_path="/jobs", status=401)
    log.append(method="POST", request_path="/jobs", status=202)
    raw = (tmp_path / ACCESS_LOG_FILENAME).read_text()
    assert "super-secret-fake-token" not in raw
    assert "marker-in-description-body" not in raw


def test_append_truncates_an_oversized_path(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    oversized = "/" + ("a" * (MAX_PATH_BYTES * 2))
    log.append(method="GET", request_path=oversized, status=200)
    line = (tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0]
    record = json.loads(line)  # must round-trip as valid JSON
    assert len(record["path"].encode("utf-8")) <= MAX_PATH_BYTES
    assert record["path"] == oversized[:MAX_PATH_BYTES]


def test_append_preserves_a_path_within_the_byte_limit(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="GET", request_path="/jobs/abc", status=200)
    record = json.loads((tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()[0])
    assert record["path"] == "/jobs/abc"


def test_append_persists_a_quote_and_newline_as_one_valid_json_line(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    tricky = '/weird"path\nwith-newline'
    log.append(method="GET", request_path=tricky, status=404)
    lines = (tmp_path / ACCESS_LOG_FILENAME).read_text().splitlines()
    assert len(lines) == 1  # the embedded newline did not split the record
    record = json.loads(lines[0])
    assert record["path"] == tricky


def test_read_access_log_returns_newest_and_skips_junk(tmp_path):
    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    for i in range(5):
        log.append(method="GET", request_path=f"/p{i}", status=200)
    target = tmp_path / ACCESS_LOG_FILENAME
    target.write_text('not json\n"a string"\n' + target.read_text())
    records = read_access_log(str(tmp_path), limit=3)
    assert [r["path"] for r in records] == ["/p2", "/p3", "/p4"]


def test_read_access_log_handles_absent_and_unreadable_logs(tmp_path, monkeypatch):
    assert read_access_log(str(tmp_path)) == []

    log = AccessLog(str(tmp_path), clock=lambda: 1.0)
    log.append(method="GET", request_path="/health", status=200)

    from pathlib import Path

    def boom(self, *a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(Path, "read_text", boom)
    assert read_access_log(str(tmp_path)) == []
