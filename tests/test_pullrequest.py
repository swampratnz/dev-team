"""Tests for the pull-request publisher (the delivery terminus primitive).

The GitHub call is exercised through an injected ``http`` fake, so nothing here
touches the network; the default urllib transport is covered by monkeypatching
``urllib.request.urlopen`` (as ``test_depscan`` does for OSV).
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import dev_team.pullrequest as pr
from dev_team.pullrequest import (
    FakePullRequestPublisher,
    GitHubPullRequestPublisher,
    PullRequest,
    PullRequestError,
    PullRequestRequest,
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
