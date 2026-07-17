"""Tests for the PR-comment interaction channel (ROADMAP #7).

The GitHub calls are exercised through injected ``http_post``/``http_get``
fakes, so nothing here touches the network; the default urllib transport is
covered by monkeypatching ``urllib.request.urlopen`` (as ``test_pullrequest``
and ``test_checks`` do for their own modules).
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from dev_team.interaction import Choice, InteractionChannel, Question, Reply
from dev_team.pr_comment_channel import GitHubPRCommentChannel, GitHubPRCommentChannelError


def _question(**over):
    base = dict(
        topic="ci-fix",
        prompt="CI is failing (test). Fix it and re-push (round 1)?",
        choices=(Choice("apply", "let the engineer fix it"), Choice("skip", "leave it")),
        context="boom",
        asked_by="Ada",
        fail_safe_key="skip",
    )
    base.update(over)
    return Question(**base)


def _channel(**over):
    base = dict(
        token="TOK", owner="acme", name="mono", pr_number=42,
        allowed_logins=("ada",),
    )
    base.update(over)
    return GitHubPRCommentChannel(**base)


class _RecordingPost:
    """Records every (url, body, headers) POST; returns a canned response."""

    def __init__(self, response=None, raises=None):
        self.response = response if response is not None else {
            "id": 1, "created_at": "2026-01-01T00:00:00Z",
        }
        self.raises = raises
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, json.loads(body.decode("utf-8")), dict(headers)))
        if self.raises is not None:
            raise self.raises
        return self.response


class _ScriptedGet:
    """Returns queued pages of comments per poll call (last page repeats)."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        return self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", {}, io.BytesIO(body)
    )


# --- ask(): posting --------------------------------------------------------


def test_ask_posts_one_comment_with_prompt_context_and_choices():
    http_post = _RecordingPost()
    http_get = _ScriptedGet([[]])
    channel = _channel(http_post=http_post, http_get=http_get, max_polls=1, sleep=lambda s: None)
    channel.ask(_question())
    assert len(http_post.calls) == 1
    url, payload, headers = http_post.calls[0]
    assert url == "https://api.github.com/repos/acme/mono/issues/42/comments"
    assert headers["Authorization"] == "Bearer TOK"
    assert "Ada asks" in payload["body"]
    assert "CI is failing" in payload["body"]
    assert "boom" in payload["body"]
    assert "`apply`" in payload["body"] and "`skip`" in payload["body"]


def test_ask_omits_context_section_when_empty():
    http_post = _RecordingPost()
    http_get = _ScriptedGet([[]])
    channel = _channel(http_post=http_post, http_get=http_get, max_polls=1, sleep=lambda s: None)
    channel.ask(_question(context=""))
    _, payload, _ = http_post.calls[0]
    assert "boom" not in payload["body"]


# --- ask(): authorized matching reply --------------------------------------


def test_ask_returns_reply_from_authorized_matching_comment():
    comments = [{"id": 2, "user": {"login": "ada"}, "body": "apply - go ahead"}]
    http_get = _ScriptedGet([comments])
    slept = []
    channel = _channel(http_post=_RecordingPost(), http_get=http_get, max_polls=5, sleep=slept.append)
    reply = channel.ask(_question())
    assert reply == Reply(choice="apply")
    assert slept == []  # matched on the first poll, no sleep needed


def test_ask_login_match_is_case_insensitive():
    comments = [{"id": 2, "user": {"login": "ADA"}, "body": "APPLY"}]
    http_get = _ScriptedGet([comments])
    channel = _channel(http_post=_RecordingPost(), http_get=http_get, max_polls=1, sleep=lambda s: None)
    reply = channel.ask(_question())
    assert reply == Reply(choice="apply")


def test_ask_ignores_the_posted_question_comment_itself():
    http_post = _RecordingPost(response={"id": 9, "created_at": "t"})
    echoed_own_comment = [{"id": 9, "user": {"login": "ada"}, "body": "apply"}]
    http_get = _ScriptedGet([echoed_own_comment, echoed_own_comment])
    channel = _channel(http_post=http_post, http_get=http_get, max_polls=2, sleep=lambda s: None)
    reply = channel.ask(_question())
    assert reply.choice == "skip"  # fail-safe: never matched its own posted comment


# --- ask(): unauthorized reply is ignored, polling continues --------------


def test_ask_ignores_unauthorized_login_and_fails_safe():
    comments = [{"id": 2, "user": {"login": "mallory"}, "body": "apply"}]
    http_get = _ScriptedGet([comments])
    channel = _channel(http_post=_RecordingPost(), http_get=http_get, max_polls=2, sleep=lambda s: None)
    reply = channel.ask(_question())
    assert reply == Reply(choice="skip")
    assert len(http_get.calls) == 2  # polling continued past the unauthorized reply


def test_ask_ignores_comment_with_no_login():
    comments = [{"id": 2, "user": {}, "body": "apply"}]
    http_get = _ScriptedGet([comments])
    channel = _channel(http_post=_RecordingPost(), http_get=http_get, max_polls=1, sleep=lambda s: None)
    assert channel.ask(_question()).choice == "skip"


def test_ask_empty_allow_list_never_matches():
    comments = [{"id": 2, "user": {"login": "ada"}, "body": "apply"}]
    http_get = _ScriptedGet([comments])
    channel = _channel(
        http_post=_RecordingPost(), http_get=http_get, allowed_logins=(), max_polls=1,
        sleep=lambda s: None,
    )
    assert channel.ask(_question()) == Reply(choice="skip")


def test_ask_ignores_blank_entries_in_allow_list():
    comments = [{"id": 2, "user": {"login": "ada"}, "body": "apply"}]
    http_get = _ScriptedGet([comments])
    channel = _channel(
        http_post=_RecordingPost(), http_get=http_get, allowed_logins=("", "  ", "ada"),
        max_polls=1, sleep=lambda s: None,
    )
    assert channel.ask(_question()) == Reply(choice="apply")


# --- ask(): unrecognised token is not fuzzy-matched ------------------------


def test_ask_ignores_unrecognised_token():
    comments = [{"id": 2, "user": {"login": "ada"}, "body": "maybe apply?"}]
    http_get = _ScriptedGet([comments])
    channel = _channel(http_post=_RecordingPost(), http_get=http_get, max_polls=1, sleep=lambda s: None)
    assert channel.ask(_question()) == Reply(choice="skip")


def test_ask_ignores_empty_comment_body():
    comments = [{"id": 2, "user": {"login": "ada"}, "body": "   "}]
    http_get = _ScriptedGet([comments])
    channel = _channel(http_post=_RecordingPost(), http_get=http_get, max_polls=1, sleep=lambda s: None)
    assert channel.ask(_question()) == Reply(choice="skip")


# --- ask(): fail-safe once the poll bound is exhausted ---------------------


def test_ask_exhausts_polls_and_returns_fail_safe():
    http_get = _ScriptedGet([[]])
    slept = []
    channel = _channel(
        http_post=_RecordingPost(), http_get=http_get, max_polls=3,
        poll_interval_seconds=5.0, sleep=slept.append,
    )
    reply = channel.ask(_question())
    assert reply == Reply(choice="skip")
    assert slept == [5.0, 5.0]  # slept before polls 2 and 3, not before the 1st


# --- since= query construction ---------------------------------------------


def test_since_query_omitted_when_post_has_no_created_at():
    http_post = _RecordingPost(response={"id": 1})
    http_get = _ScriptedGet([[]])
    channel = _channel(http_post=http_post, http_get=http_get, max_polls=1, sleep=lambda s: None)
    channel.ask(_question())
    url, _ = http_get.calls[0]
    assert "since=" not in url


def test_since_query_included_when_post_has_created_at():
    http_post = _RecordingPost(response={"id": 1, "created_at": "2026-01-01T00:00:00Z"})
    http_get = _ScriptedGet([[]])
    channel = _channel(http_post=http_post, http_get=http_get, max_polls=1, sleep=lambda s: None)
    channel.ask(_question())
    url, _ = http_get.calls[0]
    assert "since=" in url


def test_post_returns_empty_dict_on_non_dict_response():
    http_post = _RecordingPost(response=["not", "a", "dict"])
    http_get = _ScriptedGet([[]])
    channel = _channel(http_post=http_post, http_get=http_get, max_polls=1, sleep=lambda s: None)
    assert channel.ask(_question()).choice == "skip"


def test_poll_returns_empty_list_on_non_list_response():
    http_get = lambda url, headers: {"not": "a list"}
    channel = _channel(http_post=_RecordingPost(), http_get=http_get, max_polls=1, sleep=lambda s: None)
    assert channel.ask(_question()).choice == "skip"


# --- token hygiene on transport errors (AC 10: posting AND polling) -------


def test_post_transport_error_is_scrubbed_of_the_token():
    secret = "ghp_secretvalue123"
    http_post = _RecordingPost(raises=urllib.error.URLError(f"host {secret} refused"))
    channel = _channel(token=secret, http_post=http_post)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    msg = str(exc.value)
    assert "could not reach" in msg
    assert secret not in msg and "***" in msg


def test_post_http_error_with_message_is_surfaced():
    http_post = _RecordingPost(raises=_http_error(422, b'{"message": "validation failed"}'))
    channel = _channel(http_post=http_post)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    assert "422" in str(exc.value) and "validation failed" in str(exc.value)


def test_post_401_reports_auth_even_without_a_message():
    http_post = _RecordingPost(raises=_http_error(401, b"{}"))
    channel = _channel(http_post=http_post)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    assert "authentication failed" in str(exc.value).lower()


def test_poll_transport_error_is_scrubbed_of_the_token():
    secret = "ghp_secretvalue123"

    def boom(url, headers):
        raise urllib.error.URLError(f"host {secret} refused")

    channel = _channel(token=secret, http_post=_RecordingPost(), http_get=boom)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    msg = str(exc.value)
    assert "could not reach" in msg
    assert secret not in msg and "***" in msg


def test_poll_http_error_with_message_is_surfaced():
    def boom(url, headers):
        raise _http_error(404, b'{"message": "not found"}')

    channel = _channel(http_post=_RecordingPost(), http_get=boom)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    assert "404" in str(exc.value) and "not found" in str(exc.value)


def test_poll_403_reports_auth_even_without_a_message():
    def boom(url, headers):
        raise _http_error(403, b"{}")

    channel = _channel(http_post=_RecordingPost(), http_get=boom)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    assert "authentication failed" in str(exc.value).lower()


def test_post_other_code_with_unparsable_body():
    http_post = _RecordingPost(raises=_http_error(500, b"<html>not json</html>"))
    channel = _channel(http_post=http_post)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    assert "500" in str(exc.value)


def test_empty_token_scrub_leaves_message_intact():
    http_post = _RecordingPost(raises=urllib.error.URLError("down"))
    channel = _channel(token="", http_post=http_post)
    with pytest.raises(GitHubPRCommentChannelError) as exc:
        channel.ask(_question())
    assert "down" in str(exc.value)


# --- default transport (urllib) --------------------------------------------


def test_default_http_post_and_get_use_urllib(monkeypatch):
    captured = {"methods": []}

    class _PostResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"id": 1, "created_at": "t"}'

    class _GetResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"[]"

    def fake_urlopen(request, timeout=None):
        captured["methods"].append(request.get_method())
        captured["auth"] = request.headers.get("Authorization")
        if request.get_method() == "POST":
            return _PostResponse()
        return _GetResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # http_post/http_get=None -> the channel falls back to the default transports.
    channel = _channel(max_polls=1, sleep=lambda s: None)
    reply = channel.ask(_question())
    assert reply.choice == "skip"
    assert captured["methods"] == ["POST", "GET"]
    assert captured["auth"] == "Bearer TOK"


# --- repr / protocol ---------------------------------------------------------


def test_repr_does_not_leak_the_token():
    # token/http_post/http_get are field(repr=False): a traceback, logger.debug,
    # or a config dump of the channel must never print the credential verbatim.
    secret = "ghp_supersecretvalue123"
    text = repr(_channel(token=secret))
    assert secret not in text


def test_channel_satisfies_the_interaction_channel_protocol():
    assert isinstance(_channel(), InteractionChannel)
