"""Tests for the pull-request publisher (the delivery terminus primitive).

The GitHub call is exercised through an injected ``http`` fake, so nothing here
touches the network; the default urllib transport is covered by monkeypatching
``urllib.request.urlopen`` (as ``test_depscan`` does for OSV).
"""

from __future__ import annotations

import io
import itertools
import json
import urllib.error

import pytest

import dev_team.pullrequest as pr
from dev_team.pullrequest import (
    CheckRunsResult,
    FakePullRequestPublisher,
    GitHubCheckRunsClient,
    GitHubPullRequestPublisher,
    PullRequest,
    PullRequestError,
    PullRequestRequest,
    aggregate_check_runs,
)


def _req(**over):
    base = dict(
        owner="acme", name="mono", title="Add /health", body="report body",
        head="dev-team/health", base="main",
    )
    base.update(over)
    return PullRequestRequest(**base)


class _RecordingHttp:
    """Records the (url, body, headers) and returns a canned JSON response."""

    def __init__(self, response=None, raises=None):
        self.response = response if response is not None else {
            "number": 42, "html_url": "https://github.com/acme/mono/pull/42"
        }
        self.raises = raises
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, json.loads(body.decode("utf-8")), dict(headers)))
        if self.raises is not None:
            raise self.raises
        return self.response


def test_open_builds_request_and_parses_response():
    http = _RecordingHttp()
    result = GitHubPullRequestPublisher(token="TOK", http=http).open(_req())
    url, payload, headers = http.calls[-1]
    assert url == "https://api.github.com/repos/acme/mono/pulls"
    assert headers["Authorization"] == "Bearer TOK"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert payload == {
        "title": "Add /health", "body": "report body",
        "head": "dev-team/health", "base": "main", "draft": False,
    }
    assert result == PullRequest(number=42, url="https://github.com/acme/mono/pull/42")


def test_open_passes_draft_and_custom_base():
    http = _RecordingHttp()
    GitHubPullRequestPublisher(token="T", http=http).open(_req(base="develop", draft=True))
    _, payload, _ = http.calls[-1]
    assert payload["base"] == "develop"
    assert payload["draft"] is True


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", {}, io.BytesIO(body)
    )


def test_open_422_already_exists_is_a_clear_error():
    http = _RecordingHttp(
        raises=_http_error(422, b'{"message": "A pull request already exists for acme:x"}')
    )
    with pytest.raises(PullRequestError) as exc:
        GitHubPullRequestPublisher(token="T", http=http).open(_req())
    msg = str(exc.value)
    assert "422" in msg and "already exist" in msg.lower()


def test_open_401_reports_auth_even_without_a_message():
    http = _RecordingHttp(raises=_http_error(401, b'{}'))
    with pytest.raises(PullRequestError) as exc:
        GitHubPullRequestPublisher(token="T", http=http).open(_req())
    assert "authentication failed" in str(exc.value).lower()


def test_open_other_code_with_unparsable_body():
    http = _RecordingHttp(raises=_http_error(500, b"<html>not json</html>"))
    with pytest.raises(PullRequestError) as exc:
        GitHubPullRequestPublisher(token="T", http=http).open(_req())
    assert "500" in str(exc.value)


def test_open_transport_error_is_scrubbed_of_the_token():
    secret = "ghp_secretvalue123"
    http = _RecordingHttp(raises=urllib.error.URLError(f"host {secret} refused"))
    with pytest.raises(PullRequestError) as exc:
        GitHubPullRequestPublisher(token=secret, http=http).open(_req())
    msg = str(exc.value)
    assert "could not reach" in msg
    assert secret not in msg and "***" in msg


def test_open_empty_token_scrub_leaves_message_intact():
    # The token-empty branch of _scrub: no replacement, message unchanged.
    http = _RecordingHttp(raises=urllib.error.URLError("down"))
    with pytest.raises(PullRequestError) as exc:
        GitHubPullRequestPublisher(token="", http=http).open(_req())
    assert "down" in str(exc.value)


def test_open_malformed_response_is_an_error():
    http = _RecordingHttp(response={"unexpected": True})
    with pytest.raises(PullRequestError) as exc:
        GitHubPullRequestPublisher(token="T", http=http).open(_req())
    assert "unexpected response" in str(exc.value).lower()


def test_default_http_post_uses_urllib(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"number": 7, "html_url": "u"}'

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["auth"] = request.headers.get("Authorization")
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # http=None -> the publisher falls back to the default _http_post transport.
    result = GitHubPullRequestPublisher(token="TOK").open(_req())
    assert result == PullRequest(number=7, url="u")
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.github.com/repos/acme/mono/pulls"
    assert captured["auth"] == "Bearer TOK"
    assert captured["body"]["head"] == "dev-team/health"


def test_repr_does_not_leak_the_token():
    # token (and http) are field(repr=False): a traceback, logger.debug, or a
    # config dump of the publisher must never print the credential verbatim
    # (CLAUDE.md §2/§6; mirrors sdk.py's repr=False on its client).
    secret = "ghp_supersecretvalue123"
    text = repr(GitHubPullRequestPublisher(token=secret, http=_RecordingHttp()))
    assert secret not in text
    assert "***" not in text  # the field is simply absent, not masked into the repr


def test_fake_publisher_records_and_returns():
    fake = FakePullRequestPublisher(result=PullRequest(9, "https://x/9"))
    out = fake.open(_req(title="feat"))
    assert out == PullRequest(9, "https://x/9")
    assert fake.requests[-1].title == "feat"


def test_fake_publisher_raises_configured_error():
    fake = FakePullRequestPublisher(error=PullRequestError("nope"))
    with pytest.raises(PullRequestError):
        fake.open(_req())
    assert len(fake.requests) == 1  # the request is still recorded before raising


def test_publisher_satisfies_the_protocol():
    from dev_team.pullrequest import PullRequestPublisher

    assert isinstance(FakePullRequestPublisher(), PullRequestPublisher)
    assert isinstance(GitHubPullRequestPublisher(token="t"), PullRequestPublisher)
    # module import guard (keeps pr referenced for a clear failure if renamed)
    assert pr.PullRequest is PullRequest


# --- GitHubCheckRunsClient / aggregate_check_runs (issue #71) ---------------------


def _run(status="completed", conclusion="success", name="ci"):
    return {"status": status, "conclusion": conclusion, "name": name}


def test_aggregate_check_runs_no_checks_for_empty_list():
    assert aggregate_check_runs([]) == "no_checks"


def test_aggregate_check_runs_pending_when_any_run_incomplete():
    runs = [_run(), _run(status="in_progress", conclusion=None)]
    assert aggregate_check_runs(runs) == "pending"


def test_aggregate_check_runs_success_when_all_completed_ok():
    runs = [_run(conclusion="success"), _run(conclusion="neutral"), _run(conclusion="skipped")]
    assert aggregate_check_runs(runs) == "success"


def test_aggregate_check_runs_failure_when_any_conclusion_is_bad():
    runs = [_run(conclusion="success"), _run(conclusion="failure")]
    assert aggregate_check_runs(runs) == "failure"


def test_aggregate_check_runs_unfamiliar_conclusion_is_never_success():
    # Fail-secure: an unrecognised conclusion string never resolves to success.
    runs = [_run(conclusion="something_new_from_github")]
    assert aggregate_check_runs(runs) == "failure"


def test_check_runs_result_failing_names_and_to_dict():
    result = CheckRunsResult(
        state="failure",
        check_runs=[_run(conclusion="failure", name="build"), _run(conclusion="success", name="lint")],
    )
    assert result.failing_names == ["build"]
    assert result.to_dict() == {
        "state": "failure", "failing_checks": ["build"], "timed_out": False, "error": None,
    }


def _pending_http(url, headers):
    return {"check_runs": [_run(status="in_progress", conclusion=None)]}


def test_watch_polls_until_success_after_three_calls():
    calls = []

    def http(url, headers):
        calls.append(url)
        if len(calls) < 3:
            return {"check_runs": [_run(status="in_progress", conclusion=None)]}
        return {"check_runs": [_run(conclusion="success")]}

    sleeps = []
    client = GitHubCheckRunsClient(
        token="T", http=http, sleep=lambda s: sleeps.append(s), clock=lambda: 0.0,
    )
    result = client.watch("acme", "mono", "sha1")
    assert result.state == "success"
    assert len(calls) == 3  # exactly three polls, not more
    assert len(sleeps) == 2  # slept between poll 1→2 and 2→3, never after success


def test_watch_never_resolves_times_out_via_injected_clock():
    # A clock that jumps well past timeout_seconds on the very next read —
    # this never performs a real wait (sleep is a no-op) and completes fast.
    ticks = itertools.count(start=0.0, step=1000.0)
    client = GitHubCheckRunsClient(
        token="T", http=_pending_http, sleep=lambda s: None, clock=lambda: next(ticks),
    )
    result = client.watch("acme", "mono", "sha1", timeout_seconds=10, poll_interval_seconds=5)
    assert result.state == "pending"
    assert result.timed_out is True


def test_clamp_timeout_seconds_ceiling():
    assert pr._clamp_timeout_seconds(999999) == pr.MAX_CHECKS_TIMEOUT_SECONDS == 900.0


def test_clamp_poll_interval_seconds_floor():
    assert pr._clamp_poll_interval_seconds(0) == pr.MIN_CHECKS_POLL_INTERVAL_SECONDS == 5.0


def test_watch_clamps_excessive_timeout_before_polling_begins():
    # If timeout_seconds=999999 were honoured literally, elapsed=901 would
    # never trip a timeout and the finite `ticks` iterator would be exhausted
    # (StopIteration) by a second poll — proving the ceiling is applied
    # *before* polling, not just documented.
    ticks = iter([0.0, 901.0])
    client = GitHubCheckRunsClient(
        token="T", http=_pending_http, sleep=lambda s: None, clock=lambda: next(ticks),
    )
    result = client.watch("acme", "mono", "sha1", timeout_seconds=999999, poll_interval_seconds=5)
    assert result.state == "pending"
    assert result.timed_out is True


def test_watch_floors_an_excessively_small_poll_interval():
    calls = {"n": 0}

    def http(url, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"check_runs": [_run(status="in_progress", conclusion=None)]}
        return {"check_runs": [_run(conclusion="success")]}

    sleeps = []
    client = GitHubCheckRunsClient(
        token="T", http=http, sleep=lambda s: sleeps.append(s), clock=lambda: 0.0,
    )
    result = client.watch("acme", "mono", "sha1", poll_interval_seconds=0)
    assert result.state == "success"
    assert sleeps == [pr.MIN_CHECKS_POLL_INTERVAL_SECONDS]


def test_watch_403_is_caught_and_returns_unknown_with_a_message():
    error = _http_error(403, b'{"message": "Bad credentials"}')

    def http(url, headers):
        raise error

    result = GitHubCheckRunsClient(token="T", http=http).watch("acme", "mono", "sha1")
    assert result.state == "unknown"
    assert "Bad credentials" in result.error


def test_watch_401_without_a_message_uses_a_default():
    error = _http_error(401, b"{}")

    def http(url, headers):
        raise error

    result = GitHubCheckRunsClient(token="T", http=http).watch("acme", "mono", "sha1")
    assert result.state == "unknown"
    assert "authentication failed" in result.error.lower()


def test_watch_404_without_a_message_uses_a_default():
    error = _http_error(404, b"{}")

    def http(url, headers):
        raise error

    result = GitHubCheckRunsClient(token="T", http=http).watch("acme", "mono", "sha1")
    assert result.state == "unknown"
    assert "no check runs found" in result.error


def test_watch_other_code_with_unparsable_body():
    error = _http_error(500, b"<html>not json</html>")

    def http(url, headers):
        raise error

    result = GitHubCheckRunsClient(token="T", http=http).watch("acme", "mono", "sha1")
    assert result.state == "unknown"
    assert "500" in result.error


def test_watch_url_error_is_caught_and_returns_unknown():
    def http(url, headers):
        raise urllib.error.URLError("network unreachable")

    result = GitHubCheckRunsClient(token="T", http=http).watch("acme", "mono", "sha1")
    assert result.state == "unknown"
    assert "could not reach" in result.error


def test_watch_http_error_message_is_scrubbed_of_the_token():
    secret = "ghp_secretvalue123"
    error = _http_error(403, json.dumps({"message": f"token {secret} is bad"}).encode("utf-8"))

    def http(url, headers):
        raise error

    result = GitHubCheckRunsClient(token=secret, http=http).watch("acme", "mono", "sha1")
    assert secret not in result.error
    assert "***" in result.error


def test_watch_url_error_reason_is_scrubbed_of_the_token():
    secret = "ghp_secretvalue123"

    def http(url, headers):
        raise urllib.error.URLError(f"host {secret} refused")

    result = GitHubCheckRunsClient(token=secret, http=http).watch("acme", "mono", "sha1")
    assert secret not in result.error
    assert "***" in result.error


def test_watch_empty_token_scrub_leaves_message_intact():
    def http(url, headers):
        raise urllib.error.URLError("down")

    result = GitHubCheckRunsClient(token="", http=http).watch("acme", "mono", "sha1")
    assert "down" in result.error


def test_watch_no_exception_escapes_for_transport_or_auth_failures():
    for error in (
        _http_error(403, b"{}"),
        _http_error(404, b"{}"),
        urllib.error.URLError("down"),
    ):

        def http(url, headers, _error=error):
            raise _error

        # Must return a result, never raise.
        result = GitHubCheckRunsClient(token="T", http=http).watch("acme", "mono", "sha1")
        assert result.state == "unknown"


def test_default_http_get_uses_urllib(monkeypatch):
    import urllib.request

    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"check_runs": []}'

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # http=None -> the client falls back to the default _http_get transport.
    result = GitHubCheckRunsClient(token="TOK").watch("acme", "mono", "sha1")
    assert result.state == "no_checks"
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.github.com/repos/acme/mono/commits/sha1/check-runs"
    assert captured["auth"] == "Bearer TOK"


def test_check_runs_client_repr_does_not_leak_the_token():
    secret = "ghp_supersecretvalue123"
    text = repr(GitHubCheckRunsClient(token=secret, http=_pending_http))
    assert secret not in text
    assert "***" not in text  # the field is simply absent, not masked into the repr
