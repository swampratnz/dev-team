"""Tests for the PR checks watcher primitive."""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from dev_team.checks import (
    ChecksError,
    ChecksOutcome,
    GitHubChecksReader,
    _classify,
    watch_checks,
)


def _run(name, status="completed", conclusion="success", summary=None):
    run = {"name": name, "status": status, "conclusion": conclusion}
    if summary is not None:
        run["output"] = {"summary": summary}
    return run


# --- _classify ----------------------------------------------------------


def test_classify_all_passed():
    out = _classify([_run("test"), _run("lint")], "pending")  # combined pending ignored
    assert out.state == "success" and out.ok
    assert "2 check(s) passed" in out.summary


def test_classify_failure_lists_names_and_output():
    out = _classify(
        [_run("test", conclusion="failure", summary="3 tests failed"), _run("lint")],
        "success",
    )
    assert out.state == "failure" and not out.ok
    assert "test" in out.failed
    assert "3 tests failed" in out.summary


def test_classify_pending_when_a_run_is_in_progress():
    out = _classify([_run("test", status="in_progress", conclusion=None)], "pending")
    assert out.state == "pending"
    assert "test" in out.summary


def test_classify_combined_failure_fails_even_without_check_runs():
    out = _classify([], "failure")
    assert out.state == "failure"
    assert "commit status" in out.failed


def test_classify_no_checks_is_pending():
    assert _classify([], "pending").state == "pending"


def test_classify_legacy_status_only_success():
    # a repo with no check-runs but a green combined status still passes
    out = _classify([], "success")
    assert out.state == "success"
    assert "commit status" in out.summary


def test_checks_outcome_concluded():
    assert ChecksOutcome("success").concluded
    assert ChecksOutcome("failure").concluded
    assert not ChecksOutcome("pending").concluded
    assert not ChecksOutcome("timeout").concluded


# --- GitHubChecksReader --------------------------------------------------


class _FakeHttp:
    """Returns canned JSON keyed by which endpoint the URL hits (single page)."""

    def __init__(self, check_runs, combined_state):
        self._check_runs = list(check_runs)
        self._combined_state = combined_state
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        if "/check-runs" in url:
            return {"total_count": len(self._check_runs), "check_runs": self._check_runs}
        return {"state": self._combined_state}


def test_reader_status_classifies_from_both_endpoints():
    http = _FakeHttp([_run("test", conclusion="failure")], "success")
    reader = GitHubChecksReader(token="secret-tok", http=http)
    out = reader.status("o", "r", "abc123")
    assert out.state == "failure"
    # both endpoints were queried for the head ref, with a bearer token header
    assert any("/commits/abc123/check-runs" in u for u, _ in http.calls)
    assert any("/commits/abc123/status" in u for u, _ in http.calls)
    assert http.calls[0][1]["Authorization"] == "Bearer secret-tok"


def test_valid_ref_allows_shas_tags_branches_rejects_url_hostile():
    from dev_team.checks import valid_ref

    for good in ("abc123", "v1.2.3", "dev-team/feature-x", "release/1.0", "HEAD"):
        assert valid_ref(good), good
    for bad in (
        "../../../repos/x/y/commits/z",
        "..",
        "a/../b",
        "main?per_page=1",
        "a#frag",
        "a b",
        "a\\b",
        "/leading",
        "trailing/",
        "",
        "a%2fb",
    ):
        assert not valid_ref(bad), bad


def test_reader_status_rejects_a_traversal_ref_before_any_request():
    calls = []
    reader = GitHubChecksReader(
        token="t", http=lambda url, headers: calls.append(url) or {}
    )
    with pytest.raises(ChecksError) as excinfo:
        reader.status("o", "r", "../../otherorg/private/commits/main")
    assert "invalid ref" in str(excinfo.value)
    assert calls == []  # never issued a request with the hostile ref


def test_reader_paginates_and_sees_a_failure_beyond_page_one():
    # 150 runs over two pages; the failing run is on page 2 and must be seen
    page1 = [_run(f"t{i}") for i in range(100)]
    page2 = [_run(f"t{i}") for i in range(100, 149)] + [_run("late", conclusion="failure")]
    pages = []

    def http(url, headers):
        if "/check-runs" in url:
            page = int(url.split("&page=")[1])
            pages.append(page)
            return {"total_count": 150, "check_runs": page1 if page == 1 else page2}
        return {"state": "success"}

    out = GitHubChecksReader(token="t", http=http).status("o", "r", "sha")
    assert out.state == "failure" and "late" in out.failed
    assert pages == [1, 2]  # followed pagination exactly to completion


def test_reader_stops_paginating_on_empty_page():
    out = GitHubChecksReader(token="t", http=_FakeHttp([], "pending")).status("o", "r", "s")
    assert out.state == "pending"  # no runs, nothing failed


def test_reader_stops_paginating_when_total_count_absent():
    def http(url, headers):
        if "/check-runs" in url:
            return {"check_runs": [_run("t")]}  # no total_count -> stop after page 1
        return {"state": "pending"}

    out = GitHubChecksReader(token="t", http=http).status("o", "r", "s")
    assert out.state == "success"


def test_reader_rejects_non_dict_response():
    reader = GitHubChecksReader(token="t", http=lambda url, headers: ["not", "a", "dict"])
    with pytest.raises(ChecksError):
        reader.status("o", "r", "sha")


def test_reader_scrubs_token_from_transport_error():
    def boom(url, headers):
        raise urllib.error.URLError("boom secret-tok leaked")

    reader = GitHubChecksReader(token="secret-tok", http=boom)
    with pytest.raises(ChecksError) as excinfo:
        reader.status("o", "r", "sha")
    assert "secret-tok" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


def test_reader_describes_403_with_default_message():
    # an unreadable body falls back to the auth-scope default (403 branch)
    def boom(url, headers):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    reader = GitHubChecksReader(token="secret-tok", http=boom)
    with pytest.raises(ChecksError) as excinfo:
        reader.status("o", "r", "sha")
    msg = str(excinfo.value)
    assert "403" in msg and "repo read scope" in msg and "secret-tok" not in msg


def test_reader_describes_http_error_with_json_message():
    # a non-auth code with a JSON body: the message is surfaced and scrubbed
    def boom(url, headers):
        body = io.BytesIO(b'{"message": "Not Found for secret-tok"}')
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, body)

    reader = GitHubChecksReader(token="secret-tok", http=boom)
    with pytest.raises(ChecksError) as excinfo:
        reader.status("o", "r", "sha")
    msg = str(excinfo.value)
    assert "404" in msg and "Not Found" in msg and "secret-tok" not in msg


def test_default_http_get_uses_urllib(monkeypatch):
    captured = {}

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=None):
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        if "/check-runs" in request.full_url:
            return _Response(b'{"total_count": 1, "check_runs": [{"name": "t", '
                             b'"status": "completed", "conclusion": "success"}]}')
        return _Response(b'{"state": "success"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # http=None -> the reader falls back to the default _http_get transport.
    out = GitHubChecksReader(token="TOK").status("o", "r", "sha")
    assert out.state == "success"
    assert captured["method"] == "GET"
    assert captured["auth"] == "Bearer TOK"


def test_reader_surfaces_malformed_body_as_checks_error(monkeypatch):
    # a bad 200 body makes the default transport's json.loads raise
    # JSONDecodeError (a ValueError); it must become a ChecksError, not crash
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"<html>not json</html>"

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=None: _Response())
    with pytest.raises(ChecksError) as excinfo:
        GitHubChecksReader(token="secret-tok").status("o", "r", "sha")
    assert "malformed response" in str(excinfo.value)
    assert "secret-tok" not in str(excinfo.value)


# --- watch_checks --------------------------------------------------------


class _ScriptedReader:
    """Returns a queued sequence of outcomes (last repeats)."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def status(self, owner, name, ref):
        self.calls += 1
        return self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]


def test_watch_returns_on_first_conclusion():
    reader = _ScriptedReader([ChecksOutcome("success", summary="ok")])
    slept = []
    out = watch_checks(reader, "o", "r", "sha", sleep=slept.append)
    assert out.ok
    assert reader.calls == 1
    assert slept == []  # concluded on the first poll, never slept


def test_watch_polls_through_pending_then_concludes():
    reader = _ScriptedReader(
        [ChecksOutcome("pending"), ChecksOutcome("pending"), ChecksOutcome("failure", failed=("t",))]
    )
    slept = []
    out = watch_checks(reader, "o", "r", "sha", poll_interval_seconds=5.0, sleep=slept.append)
    assert out.state == "failure"
    assert reader.calls == 3
    assert slept == [5.0, 5.0]  # slept before the 2nd and 3rd polls only


def test_watch_times_out_when_never_concludes():
    reader = _ScriptedReader([ChecksOutcome("pending", summary="waiting on: test")])
    out = watch_checks(reader, "o", "r", "sha", max_polls=3, sleep=lambda _s: None)
    assert out.state == "timeout"
    assert reader.calls == 3
    assert "waiting on: test" in out.summary
