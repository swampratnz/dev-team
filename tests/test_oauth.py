"""Tests for GitHub OAuth sign-in (oauth.py)."""

from __future__ import annotations

import pytest

from dev_team.githubapp import GitHubAppError
from dev_team.oauth import (
    OAUTH_CLIENT_ID_KEY,
    OAUTH_CLIENT_SECRET_KEY,
    GitHubOAuth,
    OAuthConfig,
    OAuthError,
    resolve_oauth_config,
)
from dev_team.sources import parse_repo

CONFIG = OAuthConfig(client_id="cid", client_secret="csec")


class FakeHttp:
    """Records calls; replays queued responses (dicts or exceptions)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _sequential_tokens(prefix="tok"):
    counter = {"n": 0}

    def source():
        counter["n"] += 1
        return f"{prefix}{counter['n']}"

    return source


def _grant_and_identity(
    *, refresh="rt_1", installations=({"account": {"login": "acme"}},)
):
    grant = {"access_token": "user_at"}
    if refresh is not None:
        grant["refresh_token"] = refresh
    return (
        grant,
        {"login": "chris"},
        {"installations": list(installations)},
    )


def _signed_in(clock=lambda: 0.0, **kwargs):
    http = FakeHttp(*_grant_and_identity(**kwargs))
    oauth = GitHubOAuth(
        CONFIG, http=http, clock=clock, token_source=_sequential_tokens()
    )
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("code123", state)
    assert status == 200
    return oauth, http, payload


# --- resolve_oauth_config ---------------------------------------------------


def test_resolve_oauth_config_absent_is_none():
    assert resolve_oauth_config(None, environ={}) is None


def test_resolve_oauth_config_pops_environment_and_file_wins(tmp_path):
    env_file = tmp_path / "dev-team.env"
    env_file.write_text(f"{OAUTH_CLIENT_ID_KEY}=file-id\n")
    environ = {OAUTH_CLIENT_ID_KEY: "env-id", OAUTH_CLIENT_SECRET_KEY: "env-secret"}
    config = resolve_oauth_config(str(env_file), environ=environ)
    assert config == OAuthConfig(client_id="file-id", client_secret="env-secret")
    assert environ == {}


def test_resolve_oauth_config_half_configured_is_loud():
    with pytest.raises(OAuthError) as excinfo:
        resolve_oauth_config(None, environ={OAUTH_CLIENT_ID_KEY: "cid"})
    assert OAUTH_CLIENT_SECRET_KEY in str(excinfo.value)
    with pytest.raises(OAuthError) as excinfo:
        resolve_oauth_config(None, environ={OAUTH_CLIENT_SECRET_KEY: "cs"})
    assert OAUTH_CLIENT_ID_KEY in str(excinfo.value)


# --- login / callback -------------------------------------------------------


def test_login_url_carries_client_id_and_state():
    oauth = GitHubOAuth(CONFIG, http=FakeHttp(), token_source=_sequential_tokens())
    out = oauth.login_url()
    assert out["state"] == "tok1"
    assert "client_id=cid" in out["url"] and "state=tok1" in out["url"]


def test_callback_exchanges_code_and_creates_session():
    oauth, http, payload = _signed_in()
    assert payload["login"] == "chris"
    assert payload["installations"] == ["acme"]
    assert payload["session_token"] == "tok2"  # tok1 was the state
    exchange, user, installations = http.calls
    assert exchange["url"].endswith("/login/oauth/access_token")
    assert exchange["body"]["code"] == "code123"
    assert exchange["body"]["client_secret"] == "csec"
    assert user["headers"]["Authorization"] == "Bearer user_at"
    assert installations["url"].endswith("/user/installations")


def test_callback_rejects_unknown_and_replayed_state():
    oauth, _, _ = _signed_in()
    status, payload = oauth.handle_callback("code123", "never-issued")
    assert status == 400 and "state" in payload["error"]
    # the consumed state cannot be replayed
    status, _ = oauth.handle_callback("code123", "tok1")
    assert status == 400


def test_callback_rejects_expired_state_and_prunes_it():
    clock = {"now": 0.0}
    oauth = GitHubOAuth(
        CONFIG,
        http=FakeHttp(),
        clock=lambda: clock["now"],
        token_source=_sequential_tokens(),
    )
    state = oauth.login_url()["state"]
    clock["now"] = 601.0
    # a later login prunes the expired state before minting a new one
    oauth.login_url()
    status, _ = oauth.handle_callback("code123", state)
    assert status == 400


def test_callback_rejects_missing_code():
    oauth = GitHubOAuth(CONFIG, http=FakeHttp(), token_source=_sequential_tokens())
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("", state)
    assert status == 400 and "code" in payload["error"]


def test_callback_upstream_failure_is_a_502():
    http = FakeHttp(GitHubAppError("HTTP 500", status=500))
    oauth = GitHubOAuth(CONFIG, http=http, token_source=_sequential_tokens())
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("code123", state)
    assert status == 502 and "exchange failed" in payload["error"]


def test_callback_denied_grant_is_a_502_with_detail():
    http = FakeHttp({"error": "bad_verification_code", "error_description": "expired"})
    oauth = GitHubOAuth(CONFIG, http=http, token_source=_sequential_tokens())
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("code123", state)
    assert status == 502 and "expired" in payload["error"]


def test_callback_grant_without_detail_still_clean():
    http = FakeHttp({})
    oauth = GitHubOAuth(CONFIG, http=http, token_source=_sequential_tokens())
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("code123", state)
    assert status == 502 and "no detail" in payload["error"]


def test_callback_unidentifiable_user_is_a_502():
    http = FakeHttp({"access_token": "user_at"}, {"no": "login"})
    oauth = GitHubOAuth(CONFIG, http=http, token_source=_sequential_tokens())
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("code123", state)
    assert status == 502 and "identify" in payload["error"]


def test_callback_tolerates_malformed_installation_entries():
    http = FakeHttp(
        {"access_token": "user_at"},
        {"login": "chris"},
        {
            "installations": [
                {"account": {"login": "acme"}},
                {"account": {}},
                {"account": None},
                None,
                {"account": {"login": ""}},
            ]
        },
    )
    oauth = GitHubOAuth(CONFIG, http=http, token_source=_sequential_tokens())
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("code123", state)
    assert status == 200
    assert payload["installations"] == ["acme"]


def test_callback_tolerates_missing_installations_key():
    http = FakeHttp({"access_token": "user_at"}, {"login": "chris"}, {})
    oauth = GitHubOAuth(CONFIG, http=http, token_source=_sequential_tokens())
    state = oauth.login_url()["state"]
    status, payload = oauth.handle_callback("code123", state)
    assert status == 200 and payload["installations"] == []


# --- sessions ---------------------------------------------------------------


def test_session_for_finds_live_session_and_rejects_junk():
    oauth, _, payload = _signed_in()
    session = oauth.session_for(payload["session_token"])
    assert session is not None and session.login == "chris"
    assert oauth.session_for("nope") is None


def test_session_expiry_drops_the_session():
    clock = {"now": 0.0}
    oauth, _, payload = _signed_in(clock=lambda: clock["now"])
    clock["now"] = 8 * 3600.0 + 1
    assert oauth.session_for(payload["session_token"]) is None


def test_authorises_repo_by_installation_owner_case_insensitive():
    oauth, _, payload = _signed_in()
    session = oauth.session_for(payload["session_token"])
    assert oauth.authorises_repo(session, parse_repo("ACME/anything"))
    assert not oauth.authorises_repo(session, parse_repo("other/repo"))


# --- refresh ----------------------------------------------------------------


def test_refresh_rotates_the_session_and_resnapshots():
    oauth, http, payload = _signed_in()
    http.responses.extend(
        [
            {"access_token": "user_at2", "refresh_token": "rt_2"},
            {"login": "chris"},
            {"installations": [{"account": {"login": "acme"}},
                               {"account": {"login": "neworg"}}]},
        ]
    )
    status, renewed = oauth.refresh(payload["session_token"])
    assert status == 200
    assert renewed["session_token"] != payload["session_token"]
    assert renewed["installations"] == ["acme", "neworg"]
    assert http.calls[3]["body"]["grant_type"] == "refresh_token"
    assert http.calls[3]["body"]["refresh_token"] == "rt_1"
    # the old session token is dead, the new one lives
    assert oauth.session_for(payload["session_token"]) is None
    assert oauth.session_for(renewed["session_token"]) is not None


def test_refresh_keeps_old_refresh_token_when_none_returned():
    oauth, http, payload = _signed_in()
    http.responses.extend(
        [
            {"access_token": "user_at2"},
            {"login": "chris"},
            {"installations": []},
        ]
    )
    status, renewed = oauth.refresh(payload["session_token"])
    assert status == 200
    session = oauth.session_for(renewed["session_token"])
    assert session.refresh_token == "rt_1"


def test_refresh_unknown_session_is_401():
    oauth, _, _ = _signed_in()
    status, payload = oauth.refresh("bogus")
    assert status == 401


def test_refresh_without_refresh_token_is_409():
    oauth, _, payload = _signed_in(refresh=None)
    status, out = oauth.refresh(payload["session_token"])
    assert status == 409 and "sign in again" in out["error"]


def test_refresh_upstream_failure_is_a_502():
    oauth, http, payload = _signed_in()
    http.responses.append(GitHubAppError("HTTP 503", status=503))
    status, out = oauth.refresh(payload["session_token"])
    assert status == 502 and "refresh failed" in out["error"]
    # the original session survives a failed refresh
    assert oauth.session_for(payload["session_token"]) is not None
