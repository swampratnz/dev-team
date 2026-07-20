"""GitHub OAuth sign-in for the dispatch service.

This authenticates the **human**, never the agents (CLAUDE.md section 7):
a user signs in with GitHub, and the service learns which GitHub App
installations that user can reach — the set of organisations/accounts the
service may let them point jobs at. The user's OAuth tokens are used for
exactly two API calls (who are you; which installations) and are **not
retained** — only the refresh token is kept, server-side and in memory, so
the session can be renewed without a fresh browser round-trip. Repo access
for jobs always comes from the App's own installation tokens
(:mod:`dev_team.githubapp`), never from the user's grant.

Configuration mirrors the App: ``GITHUB_OAUTH_CLIENT_ID`` and
``GITHUB_OAUTH_CLIENT_SECRET`` in the same env-file search, both popped
from the process environment when found there. When unconfigured, the
dispatch service runs exactly as before — the static operator bearer token
is the only credential.

Session model:

- ``GET /auth/login`` hands back the GitHub authorisation URL plus a
  single-use ``state`` (CSRF token, 10-minute expiry).
- ``GET /auth/callback?code=…&state=…`` exchanges the code, identifies the
  user, snapshots their installations, and answers with an opaque
  **session token** the caller then presents as ``Authorization: Bearer``.
- Sessions expire (8 hours, matching GitHub's user-token lifetime) and can
  be renewed via ``POST /auth/refresh`` while the stored refresh token is
  valid; renewal **rotates** the session token and re-snapshots the
  installation set, so a revoked installation drops out at the next
  refresh rather than living for ever.
- Everything is in memory: a service restart signs everyone out (matching
  the in-memory job registry) — sign in again.

What a session may do is decided by the dispatch handler (operator-only
routes stay operator-only); what repos it may target is decided here:
:meth:`GitHubOAuth.authorises_repo` checks the submitted repo's owner
against the session's installation accounts.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, MutableMapping, Optional, Tuple

from .errors import DevTeamError
from .githubapp import GitHubAppError, HttpJson, _default_http
from .sources import RepoRef, is_github_repo, load_env_file

OAUTH_CLIENT_ID_KEY = "GITHUB_OAUTH_CLIENT_ID"
OAUTH_CLIENT_SECRET_KEY = "GITHUB_OAUTH_CLIENT_SECRET"

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"

#: A login state (CSRF token) is single-use and short-lived.
_STATE_TTL_SECONDS = 600.0

#: Session lifetime — GitHub user-to-server tokens live 8 hours; the session
#: mirrors that so an expired session and an expired user grant coincide.
_SESSION_TTL_SECONDS = 8 * 3600.0


class OAuthError(DevTeamError):
    """GitHub OAuth configuration or flow failure."""


@dataclass(frozen=True)
class OAuthConfig:
    """The OAuth app's client credentials (already resolved)."""

    client_id: str
    # Hidden from repr/traceback dumps — a secret, unlike the client id.
    client_secret: str = field(repr=False)


def resolve_oauth_config(
    env_file: Optional[str] = None,
    *,
    environ: Optional[MutableMapping[str, str]] = None,
) -> Optional[OAuthConfig]:
    """The configured OAuth client, or ``None`` when not configured.

    Same contract as the App credentials: an env file wins, both keys are
    always popped from the process environment, and half-configuration is a
    loud error rather than a silent fallback.
    """

    live = os.environ if environ is None else environ
    keys = (OAUTH_CLIENT_ID_KEY, OAUTH_CLIENT_SECRET_KEY)
    values: Dict[str, Optional[str]] = {key: live.pop(key, None) for key in keys}
    if env_file is not None:
        from_file = load_env_file(env_file)
        for key in keys:
            if from_file.get(key):
                values[key] = from_file[key]
    client_id = values[OAUTH_CLIENT_ID_KEY]
    client_secret = values[OAUTH_CLIENT_SECRET_KEY]
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        missing = OAUTH_CLIENT_ID_KEY if not client_id else OAUTH_CLIENT_SECRET_KEY
        raise OAuthError(
            f"GitHub OAuth half-configured: {missing} is missing "
            f"(set both {OAUTH_CLIENT_ID_KEY} and {OAUTH_CLIENT_SECRET_KEY}, "
            "or neither)"
        )
    return OAuthConfig(client_id=client_id, client_secret=client_secret)


@dataclass
class Session:
    """An authenticated user: who they are and what they may target.

    ``token`` and ``refresh_token`` are bearer credentials, kept out of any
    repr/traceback/log dump (``repr=False``) — the same guard the rest of
    this PR applies to secrets. They stay required positional fields:
    ``repr=False`` without a default does not change the constructor.
    """

    token: str = field(repr=False)
    login: str = field()
    installations: Tuple[str, ...] = field()
    refresh_token: Optional[str] = field(repr=False)
    expires: float = field()


class GitHubOAuth:
    """The OAuth flow + in-memory session registry, socket-free and testable.

    All GitHub traffic rides the injectable :data:`~dev_team.githubapp.HttpJson`
    transport, so unit tests never touch the network. Thread-safe: handler
    threads call every method concurrently.
    """

    def __init__(
        self,
        config: OAuthConfig,
        *,
        http: Optional[HttpJson] = None,
        clock: Callable[[], float] = time.time,
        token_source: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self._config = config
        self._http = http or _default_http
        self._clock = clock
        self._token_source = token_source
        self._lock = threading.Lock()
        self._states: Dict[str, float] = {}
        self._sessions: Dict[str, Session] = {}

    # -- login flow ----------------------------------------------------------

    def login_url(self) -> Dict[str, str]:
        """A fresh authorisation URL + its single-use ``state``."""

        state = self._token_source()
        now = self._clock()
        with self._lock:
            self._prune_states(now)
            self._states[state] = now
        url = (
            f"{_AUTHORIZE_URL}?client_id={self._config.client_id}"
            f"&state={state}"
        )
        return {"url": url, "state": state}

    def _prune_states(self, now: float) -> None:
        expired = [s for s, at in self._states.items() if now - at > _STATE_TTL_SECONDS]
        for state in expired:
            del self._states[state]

    def handle_callback(self, code: str, state: str) -> Tuple[int, Dict[str, object]]:
        """Exchange ``code`` for a session; returns ``(status, payload)``.

        The ``state`` must be one this service minted, unexpired, and is
        consumed either way (single-use, so a replayed callback fails).
        """

        now = self._clock()
        with self._lock:
            issued = self._states.pop(state, None)
        if issued is None or now - issued > _STATE_TTL_SECONDS:
            return 400, {"error": "unknown or expired state"}
        if not code:
            return 400, {"error": "missing code"}
        try:
            grant = self._exchange(
                {
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "code": code,
                }
            )
            login, installations = self._identify(grant["access_token"])
        except (GitHubAppError, OAuthError) as exc:
            return 502, {"error": f"GitHub OAuth exchange failed: {exc}"}
        session = Session(
            token=self._token_source(),
            login=login,
            installations=installations,
            refresh_token=grant.get("refresh_token"),
            expires=now + _SESSION_TTL_SECONDS,
        )
        with self._lock:
            self._sessions[session.token] = session
        return 200, self._session_payload(session)

    def refresh(self, session_token: str) -> Tuple[int, Dict[str, object]]:
        """Renew a session from its stored refresh token; rotates the token."""

        session = self.session_for(session_token)
        if session is None:
            return 401, {"error": "unauthorized"}
        if not session.refresh_token:
            return 409, {"error": "session has no refresh token; sign in again"}
        try:
            grant = self._exchange(
                {
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": session.refresh_token,
                }
            )
            login, installations = self._identify(grant["access_token"])
        except (GitHubAppError, OAuthError) as exc:
            return 502, {"error": f"GitHub OAuth refresh failed: {exc}"}
        renewed = Session(
            token=self._token_source(),
            login=login,
            installations=installations,
            refresh_token=grant.get("refresh_token", session.refresh_token),
            expires=self._clock() + _SESSION_TTL_SECONDS,
        )
        with self._lock:
            self._sessions.pop(session.token, None)
            self._sessions[renewed.token] = renewed
        return 200, self._session_payload(renewed)

    # -- session lookup / authorisation --------------------------------------

    def session_for(self, presented: str) -> Optional[Session]:
        """The live session for a presented bearer value, or ``None``.

        Each candidate token is compared with :func:`hmac.compare_digest`,
        so the comparison itself is constant-time; the *number* of iterations
        (and the dict lookup for state pruning) is not, which is an accepted
        trade-off — session and CSRF-state tokens are 256-bit
        ``secrets.token_urlsafe`` values, so the count/timing of live
        sessions leaks nothing that helps guess one. Expired sessions are
        dropped on sight.
        """

        now = self._clock()
        presented_bytes = presented.encode("utf-8")
        with self._lock:
            for token, session in list(self._sessions.items()):
                if session.expires <= now:
                    del self._sessions[token]
                    continue
                if hmac.compare_digest(presented_bytes, token.encode("utf-8")):
                    return session
        return None

    def authorises_repo(self, session: Session, ref: RepoRef) -> bool:
        """Whether the session may target ``ref``.

        Two conditions, both required: the repo must actually be on
        github.com (``is_github_repo``), and its owner must be one of the
        session's App installations. The host check is what makes the owner
        comparison a real boundary — without it a session member of org
        ``acme`` could submit ``https://internal-git.corp/acme/x`` (or an
        ``ssh://…/acme/x``) and pass on the ``owner`` path segment alone,
        pointing the clone/agent pipeline at an arbitrary internal or
        external host (SSRF / off-tenant content). A session's authority is
        a github.com installation membership, so anything off github.com is
        outside their tenant by construction.
        """

        if not is_github_repo(ref):
            return False
        owner = ref.owner.lower()
        return any(owner == account.lower() for account in session.installations)

    # -- GitHub plumbing -----------------------------------------------------

    def _session_payload(self, session: Session) -> Dict[str, object]:
        return {
            "session_token": session.token,
            "login": session.login,
            "installations": list(session.installations),
            "expires": session.expires,
        }

    def _exchange(self, body: Mapping[str, str]) -> Dict[str, str]:
        response = self._http(
            "POST",
            _ACCESS_TOKEN_URL,
            {"Accept": "application/json", "Content-Type": "application/json"},
            dict(body),
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            detail = response.get("error_description") or response.get("error")
            raise OAuthError(f"no access token granted ({detail or 'no detail'})")
        out: Dict[str, str] = {"access_token": token}
        refresh = response.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            out["refresh_token"] = refresh
        return out

    def _identify(self, user_token: str) -> Tuple[str, Tuple[str, ...]]:
        """``(login, installation account logins)`` for the user's grant."""

        headers = {
            "Authorization": f"Bearer {user_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        user = self._http("GET", f"{_API_BASE}/user", headers, None)
        login = user.get("login")
        if not isinstance(login, str) or not login:
            raise OAuthError("could not identify the signed-in user")
        return login, self._installation_accounts(headers)

    def _installation_accounts(self, headers: Mapping[str, str]) -> Tuple[str, ...]:
        """Every installation-account login for the user, following pages.

        ``GET /user/installations`` returns 30 per page by default; a user in
        many orgs would otherwise silently lose tenant access to the repos
        past page one. Request the max page size and keep paging until the
        collected count reaches the reported ``total_count`` (or a page comes
        back empty) — the same discipline
        :meth:`GitHubChecksReader._all_check_runs` uses.
        """

        accounts: list = []
        page = 1
        while True:
            listing = self._http(
                "GET",
                f"{_API_BASE}/user/installations?per_page=100&page={page}",
                dict(headers),
                None,
            )
            batch = listing.get("installations")
            batch = batch if isinstance(batch, list) else []
            for installation in batch:
                account = (installation or {}).get("account") or {}
                account_login = account.get("login")
                if isinstance(account_login, str) and account_login:
                    accounts.append(account_login)
            total = listing.get("total_count")
            if not batch or not isinstance(total, int) or len(accounts) >= total:
                return tuple(accounts)
            page += 1


__all__ = [
    "OAUTH_CLIENT_ID_KEY",
    "OAUTH_CLIENT_SECRET_KEY",
    "GitHubOAuth",
    "OAuthConfig",
    "OAuthError",
    "Session",
    "resolve_oauth_config",
]
