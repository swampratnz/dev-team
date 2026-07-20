"""Tests for GitHub App authentication (githubapp.py)."""

from __future__ import annotations

import io
import urllib.error

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dev_team.githubapp import (
    APP_ID_KEY,
    APP_KEY_FILE_KEY,
    AppCredentials,
    GitHubAppError,
    GitHubAppTokenProvider,
    _default_http,
    _parse_expiry,
    app_jwt,
    resolve_app_credentials,
    resolve_token_provider,
)
from dev_team.sources import StaticTokenProvider, parse_repo

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = _KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()

CREDS = AppCredentials(app_id="1234", private_key_pem=PRIVATE_PEM)
REF = parse_repo("acme/rota")


def _key_file(tmp_path):
    path = tmp_path / "app.pem"
    path.write_text(PRIVATE_PEM)
    return str(path)


# --- resolve_app_credentials ------------------------------------------------


def test_resolve_app_credentials_absent_is_none():
    assert resolve_app_credentials(None, environ={}) is None


def test_resolve_app_credentials_pops_process_environment(tmp_path):
    environ = {APP_ID_KEY: "77", APP_KEY_FILE_KEY: _key_file(tmp_path)}
    creds = resolve_app_credentials(None, environ=environ)
    assert creds == AppCredentials(app_id="77", private_key_pem=PRIVATE_PEM)
    assert environ == {}


def test_resolve_app_credentials_env_file_wins(tmp_path):
    key_file = _key_file(tmp_path)
    env_file = tmp_path / "dev-team.env"
    env_file.write_text(f"{APP_ID_KEY}=99\n{APP_KEY_FILE_KEY}={key_file}\n")
    environ = {APP_ID_KEY: "11", APP_KEY_FILE_KEY: "/nonexistent"}
    creds = resolve_app_credentials(str(env_file), environ=environ)
    assert creds.app_id == "99"
    assert environ == {}  # popped even when the file supplied the values


def test_resolve_app_credentials_file_and_environment_combine(tmp_path):
    # The env file names only the app id; the key-file path comes from the
    # process environment — partial file config must not mask inherited keys.
    env_file = tmp_path / "dev-team.env"
    env_file.write_text(f"{APP_ID_KEY}=55\n")
    environ = {APP_KEY_FILE_KEY: _key_file(tmp_path)}
    creds = resolve_app_credentials(str(env_file), environ=environ)
    assert creds.app_id == "55"
    assert creds.private_key_pem == PRIVATE_PEM


def test_resolve_app_credentials_half_configured_is_loud(tmp_path):
    with pytest.raises(GitHubAppError) as excinfo:
        resolve_app_credentials(None, environ={APP_ID_KEY: "77"})
    assert APP_KEY_FILE_KEY in str(excinfo.value)
    with pytest.raises(GitHubAppError) as excinfo:
        resolve_app_credentials(
            None, environ={APP_KEY_FILE_KEY: _key_file(tmp_path)}
        )
    assert APP_ID_KEY in str(excinfo.value)


def test_resolve_app_credentials_unreadable_key_file_is_loud():
    environ = {APP_ID_KEY: "77", APP_KEY_FILE_KEY: "/no/such/key.pem"}
    with pytest.raises(GitHubAppError) as excinfo:
        resolve_app_credentials(None, environ=environ)
    assert "cannot read" in str(excinfo.value)


# --- app_jwt ----------------------------------------------------------------


def test_app_jwt_signs_a_verifiable_rs256_token():
    token = app_jwt(CREDS, now=1_000_000)
    claims = pyjwt.decode(
        token,
        PUBLIC_PEM,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "1234"}


def test_app_jwt_defaults_now_to_wall_clock():
    claims = pyjwt.decode(
        app_jwt(CREDS), PUBLIC_PEM, algorithms=["RS256"]
    )
    assert claims["exp"] - claims["iat"] == 600


def test_app_jwt_bad_key_is_a_githubapp_error():
    bad = AppCredentials(app_id="1", private_key_pem="not a pem")
    with pytest.raises(GitHubAppError) as excinfo:
        app_jwt(bad, now=0)
    assert "cannot sign" in str(excinfo.value)


# --- _default_http / _parse_expiry ------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_default_http_posts_json_and_parses(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["body"] = request.data
        seen["timeout"] = timeout
        return _Response(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = _default_http("POST", "https://api.github.com/x", {"A": "b"}, {"k": 1})
    assert out == {"ok": True}
    assert seen["method"] == "POST" and seen["body"] == b'{"k": 1}'


def test_default_http_get_sends_no_body(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout: _Response(b"{}")
    )
    assert _default_http("GET", "https://api.github.com/x", {}, None) == {}


def test_default_http_http_error_carries_status(monkeypatch):
    def fail(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 404, "nf", None, io.BytesIO(b"missing")
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(GitHubAppError) as excinfo:
        _default_http("GET", "https://api.github.com/x", {}, None)
    assert excinfo.value.status == 404 and "missing" in str(excinfo.value)


def test_default_http_network_error(monkeypatch):
    def fail(request, timeout):
        raise urllib.error.URLError("dns down")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(GitHubAppError) as excinfo:
        _default_http("GET", "https://api.github.com/x", {}, None)
    assert excinfo.value.status is None and "unreachable" in str(excinfo.value)


def test_parse_expiry_iso_and_fallbacks():
    assert _parse_expiry("1970-01-01T01:00:00+00:00", now=0.0) == 3600.0
    assert _parse_expiry("2016-07-11T22:14:10Z", now=0.0) > 0
    assert _parse_expiry(None, now=100.0) == 1900.0
    assert _parse_expiry("garbage", now=100.0) == 1900.0


# --- GitHubAppTokenProvider -------------------------------------------------


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


def _install_and_token(expires="2100-01-01T00:00:00+00:00"):
    return (
        {"id": 42},
        {"token": "ghs_minted", "expires_at": expires},
    )


def test_provider_mints_a_repo_scoped_installation_token():
    http = FakeHttp(*_install_and_token())
    provider = GitHubAppTokenProvider(CREDS, http=http, clock=lambda: 1_000.0)
    assert provider.token_for(REF) == "ghs_minted"
    lookup, mint = http.calls
    assert lookup["method"] == "GET"
    assert lookup["url"].endswith("/repos/acme/rota/installation")
    assert lookup["headers"]["Authorization"].startswith("Bearer ")
    assert mint["method"] == "POST"
    assert mint["url"].endswith("/app/installations/42/access_tokens")
    assert mint["body"] == {"repositories": ["rota"]}


def test_provider_caches_until_near_expiry_then_reminting():
    clock = {"now": 0.0}
    http = FakeHttp(
        *_install_and_token(expires="1970-01-01T01:00:00+00:00"),  # exp 3600
        *_install_and_token(expires="1970-01-01T02:00:00+00:00"),
    )
    provider = GitHubAppTokenProvider(CREDS, http=http, clock=lambda: clock["now"])
    assert provider.token_for(REF) == "ghs_minted"
    assert provider.token_for(REF) == "ghs_minted"
    assert len(http.calls) == 2  # cache hit: no extra HTTP
    clock["now"] = 3400.0  # inside the 300s refresh margin of exp 3600
    assert provider.token_for(REF) == "ghs_minted"
    assert len(http.calls) == 4  # re-minted


def test_provider_non_github_ref_is_anonymous():
    ref = parse_repo("https://gitlab.example/acme/rota.git")
    http = FakeHttp()
    provider = GitHubAppTokenProvider(CREDS, http=http)
    assert provider.token_for(ref) is None
    assert http.calls == []


def test_provider_not_installed_is_a_clear_error():
    http = FakeHttp(GitHubAppError("HTTP 404", status=404))
    provider = GitHubAppTokenProvider(CREDS, http=http, clock=lambda: 0.0)
    with pytest.raises(GitHubAppError) as excinfo:
        provider.token_for(REF)
    assert "not installed on acme/rota" in str(excinfo.value)


def test_provider_other_api_error_passes_through():
    http = FakeHttp(GitHubAppError("HTTP 500", status=500))
    provider = GitHubAppTokenProvider(CREDS, http=http, clock=lambda: 0.0)
    with pytest.raises(GitHubAppError) as excinfo:
        provider.token_for(REF)
    assert "HTTP 500" in str(excinfo.value)


def test_provider_malformed_installation_response():
    http = FakeHttp({"id": "not-an-int"})
    provider = GitHubAppTokenProvider(CREDS, http=http, clock=lambda: 0.0)
    with pytest.raises(GitHubAppError) as excinfo:
        provider.token_for(REF)
    assert "no integer id" in str(excinfo.value)


def test_provider_malformed_token_response():
    http = FakeHttp({"id": 42}, {"token": ""})
    provider = GitHubAppTokenProvider(CREDS, http=http, clock=lambda: 0.0)
    with pytest.raises(GitHubAppError) as excinfo:
        provider.token_for(REF)
    assert "no token" in str(excinfo.value)


# --- resolve_token_provider -------------------------------------------------


def test_provider_mints_distinct_repos_concurrently():
    # The lock must never be held across the network mint: two threads
    # minting for DIFFERENT repos must overlap, or --max-concurrent-jobs and
    # /checks would serialise every tenant's credential behind one lock.
    import threading

    both_in = threading.Barrier(2, timeout=5)

    def slow_http(method, url, headers, body):
        if url.endswith("/installation"):
            both_in.wait()  # only clears if two mints run at once
            return {"id": 1}
        return {"token": "ghs_x", "expires_at": "2100-01-01T00:00:00+00:00"}

    provider = GitHubAppTokenProvider(CREDS, http=slow_http, clock=lambda: 0.0)
    results = {}

    def mint(slug):
        results[slug] = provider.token_for(parse_repo(slug))

    threads = [
        threading.Thread(target=mint, args=(slug,))
        for slug in ("acme/one", "acme/two")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == {"acme/one": "ghs_x", "acme/two": "ghs_x"}


def test_provider_cache_race_tolerates_a_duplicate_mint():
    # Two concurrent cache-misses for the SAME repo may both mint; that is
    # acceptable (GitHub issues independent tokens, last write wins) — the
    # point is neither thread blocks the other on the network.
    import threading

    both_in = threading.Barrier(2, timeout=5)
    mints = []

    def slow_http(method, url, headers, body):
        if url.endswith("/installation"):
            both_in.wait()
            return {"id": 1}
        mints.append(url)
        return {"token": "ghs_x", "expires_at": "2100-01-01T00:00:00+00:00"}

    provider = GitHubAppTokenProvider(CREDS, http=slow_http, clock=lambda: 0.0)

    def mint():
        provider.token_for(REF)

    threads = [threading.Thread(target=mint) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(mints) == 2  # both minted concurrently, neither blocked
    # the cache settled on a valid token; a later call is served from it
    assert provider.token_for(REF) == "ghs_x"
    assert len(mints) == 2  # no third mint — cache hit


def test_resolve_token_provider_prefers_the_app(tmp_path):
    environ = {
        APP_ID_KEY: "77",
        APP_KEY_FILE_KEY: _key_file(tmp_path),
        "GITHUB_TOKEN": "ghp_leftover",
    }
    provider = resolve_token_provider(None, environ=environ, http=FakeHttp())
    assert isinstance(provider, GitHubAppTokenProvider)
    assert environ == {}  # the leftover PAT is still popped for hygiene


def test_resolve_token_provider_falls_back_to_static_pat():
    provider = resolve_token_provider(None, environ={"GITHUB_TOKEN": "ghp_x"})
    assert provider == StaticTokenProvider("ghp_x")
    assert provider.token_for(REF) == "ghp_x"


def test_resolve_token_provider_anonymous():
    provider = resolve_token_provider(None, environ={})
    assert provider == StaticTokenProvider(None)
    assert provider.token_for(REF) is None
