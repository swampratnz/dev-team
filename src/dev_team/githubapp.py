"""GitHub App authentication: per-installation access tokens, minted on demand.

The static-PAT model (:func:`~dev_team.sources.resolve_github_token`) gives
every run one long-lived credential with one shared blast radius. A GitHub
App replaces that with the App's *own identity* (CLAUDE.md section 7: agents
never operate on a human's delegated token): the service signs a short-lived
JWT with the App's private key, exchanges it for a **repo-scoped,
one-hour installation access token**, and hands that to the same
per-command git plumbing the PAT used. Nothing long-lived is ever stored —
tokens are re-minted from the private key as needed.

Configuration comes from the same env-file search the PAT uses
(``./.env`` → ``~/.config/dev-team/dev-team.env`` → ``/etc/dev-team/…``):

- ``GITHUB_APP_ID`` — the App's numeric id.
- ``GITHUB_APP_PRIVATE_KEY_FILE`` — path to the App's PEM private key. The
  key itself never sits in an environment variable; the file should be
  root-readable-only, exactly like the env file that names it.

Both keys are **popped from the process environment** when found there, so
commands the engines later execute (gates, build probes, the code under
audit) can never read them — the same hygiene the PAT path applies.

Token hygiene downstream is unchanged: the minted token rides only in
per-command ``GIT_CONFIG_*`` variables or ``Authorization`` headers
(:func:`~dev_team.sources.git_auth_env`), and is scrubbed from any error
text (:func:`~dev_team.sources.scrub_credentials`).

The PyJWT/cryptography pair used to sign the App JWT lives behind the
``dev-team[github]`` extra and is imported lazily, so the core package
never requires it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Mapping, MutableMapping, Optional

from .errors import DevTeamError
from .sources import (
    RepoRef,
    StaticTokenProvider,
    TokenProvider,
    _is_github_https,
    load_env_file,
    resolve_github_token,
)

#: Environment (file or process) keys configuring the App.
APP_ID_KEY = "GITHUB_APP_ID"
APP_KEY_FILE_KEY = "GITHUB_APP_PRIVATE_KEY_FILE"

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_HTTP_TIMEOUT_SECONDS = 30.0

#: App JWTs are capped at 10 minutes by GitHub; stay comfortably inside, and
#: back-date ``iat`` to absorb clock skew (GitHub's own recommendation).
_JWT_IAT_SKEW_SECONDS = 60
_JWT_TTL_SECONDS = 540

#: A cached installation token is re-minted this long before it expires, so a
#: token handed to a clone or API call never dies mid-operation.
_REFRESH_MARGIN_SECONDS = 300

#: Fallback lifetime when GitHub's ``expires_at`` is missing or unparseable —
#: conservative (shorter than the documented hour) rather than optimistic.
_DEFAULT_TTL_SECONDS = 1800

#: Injectable JSON transport: ``(method, url, headers, body) -> response``.
#: Raises :class:`GitHubAppError` (with ``status``) on an HTTP error.
HttpJson = Callable[[str, str, Mapping[str, str], Optional[Dict]], Dict]


class GitHubAppError(DevTeamError):
    """GitHub App authentication failed (config, signing, or the API)."""

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AppCredentials:
    """The App's identity: its id and PEM private key (already read)."""

    app_id: str
    # Never let a repr/traceback/config dump print the private key — it is the
    # App's most sensitive credential (it mints installation tokens for every
    # repo the App can reach). Mirrors GitHubChecksReader.token's guard.
    private_key_pem: str = field(repr=False)


def resolve_app_credentials(
    env_file: Optional[str] = None,
    *,
    environ: Optional[MutableMapping[str, str]] = None,
) -> Optional[AppCredentials]:
    """The configured App credentials, or ``None`` when not configured.

    Mirrors :func:`~dev_team.sources.resolve_github_token`: an env file wins
    over the process environment, and both keys are always **popped** from
    the process environment so downstream commands never inherit them. A
    configured-but-unreadable key file is a loud error — a half-configured
    App must never silently degrade to anonymous access.
    """

    live = os.environ if environ is None else environ
    inherited = {key: live.pop(key, None) for key in (APP_ID_KEY, APP_KEY_FILE_KEY)}
    values: Dict[str, Optional[str]] = dict(inherited)
    if env_file is not None:
        from_file = load_env_file(env_file)
        for key in (APP_ID_KEY, APP_KEY_FILE_KEY):
            if from_file.get(key):
                values[key] = from_file[key]
    app_id, key_file = values[APP_ID_KEY], values[APP_KEY_FILE_KEY]
    if not app_id and not key_file:
        return None
    if not app_id or not key_file:
        missing = APP_ID_KEY if not app_id else APP_KEY_FILE_KEY
        raise GitHubAppError(
            f"GitHub App half-configured: {missing} is missing "
            f"(set both {APP_ID_KEY} and {APP_KEY_FILE_KEY}, or neither)"
        )
    try:
        with open(key_file, "r", encoding="utf-8") as handle:
            pem = handle.read()
    except OSError as exc:
        raise GitHubAppError(
            f"cannot read GitHub App private key file {key_file!r}: {exc}"
        ) from exc
    return AppCredentials(app_id=app_id, private_key_pem=pem)


def app_jwt(credentials: AppCredentials, *, now: Optional[float] = None) -> str:
    """A short-lived RS256 JWT authenticating *as the App*."""

    try:
        import jwt as pyjwt
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise GitHubAppError(
            "GitHub App auth needs PyJWT and cryptography: "
            "pip install 'dev-team[github]'"
        ) from exc
    issued = int(time.time() if now is None else now)
    payload = {
        "iat": issued - _JWT_IAT_SKEW_SECONDS,
        "exp": issued + _JWT_TTL_SECONDS,
        "iss": credentials.app_id,
    }
    try:
        return pyjwt.encode(payload, credentials.private_key_pem, algorithm="RS256")
    except Exception as exc:
        raise GitHubAppError(f"cannot sign GitHub App JWT: {exc}") from exc


def _default_http(
    method: str, url: str, headers: Mapping[str, str], body: Optional[Dict]
) -> Dict:
    """POST/GET JSON to the GitHub API; errors are scrubbed of credentials."""

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise GitHubAppError(
            f"GitHub API {method} {url} failed: HTTP {exc.code} {detail}",
            status=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise GitHubAppError(f"GitHub API unreachable: {exc.reason}") from exc


def _parse_expiry(expires_at: object, now: float) -> float:
    """``expires_at`` (ISO 8601) as an epoch, conservatively defaulted."""

    if isinstance(expires_at, str):
        try:
            return datetime.fromisoformat(expires_at).timestamp()
        except ValueError:
            pass
    return now + _DEFAULT_TTL_SECONDS


class GitHubAppTokenProvider:
    """Mints repo-scoped installation tokens, cached until near expiry.

    ``token_for`` resolves the repo's installation and mints a token scoped
    to **that single repository** (least privilege: an installation may span
    an org, but each minted credential can touch only the repo it was minted
    for). Tokens are cached per repo and re-minted inside
    :data:`_REFRESH_MARGIN_SECONDS` of expiry, so long runs never hold a
    dead credential. Thread-safe: the dispatch service calls this from
    worker and handler threads alike.

    A non-GitHub ref returns ``None`` (anonymous access) — App credentials
    are only ever presented to ``github.com``, mirroring the PAT path's
    host allowlist.
    """

    def __init__(
        self,
        credentials: AppCredentials,
        *,
        http: Optional[HttpJson] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credentials = credentials
        self._http = http or _default_http
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: Dict[str, tuple] = {}

    def _headers(self, bearer: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
            "Content-Type": "application/json",
        }

    def token_for(self, ref: RepoRef) -> Optional[str]:
        if not _is_github_https(ref.url):
            return None
        now = self._clock()
        # Lock only the cache read/write — never the network mint. This is a
        # single process-wide instance shared by every worker thread and
        # every GET /checks request, so holding the lock across _mint's two
        # blocking GitHub round-trips would serialise ALL repos' minting
        # behind one lock (and let one tenant's slow installation lookup
        # stall dispatch and /checks for every other tenant), defeating
        # --max-concurrent-jobs. Mirrors GitHubOAuth, which does its network
        # calls unlocked. The cost is a possible duplicate mint when two
        # threads miss the cache for the same repo at once — harmless: GitHub
        # issues independent tokens and the last write wins, so distinct
        # repos never block each other on the credential path.
        with self._lock:
            cached = self._tokens.get(ref.slug)
        if cached is not None and cached[1] - _REFRESH_MARGIN_SECONDS > now:
            return cached[0]
        token, expires = self._mint(ref, now)
        with self._lock:
            self._tokens[ref.slug] = (token, expires)
        return token

    def _mint(self, ref: RepoRef, now: float) -> tuple:
        """Resolve the installation and mint a token scoped to ``ref``."""

        jwt_headers = self._headers(app_jwt(self._credentials, now=now))
        try:
            installation = self._http(
                "GET",
                f"{_API_BASE}/repos/{ref.owner}/{ref.name}/installation",
                jwt_headers,
                None,
            )
        except GitHubAppError as exc:
            if exc.status == 404:
                raise GitHubAppError(
                    f"the GitHub App is not installed on {ref.slug} "
                    "(install it on the repository or organisation first)",
                    status=404,
                ) from exc
            raise
        installation_id = installation.get("id")
        if not isinstance(installation_id, int):
            raise GitHubAppError(
                f"unexpected installation response for {ref.slug}: no integer id"
            )
        minted = self._http(
            "POST",
            f"{_API_BASE}/app/installations/{installation_id}/access_tokens",
            jwt_headers,
            {"repositories": [ref.name]},
        )
        token = minted.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError(
                f"unexpected access-token response for {ref.slug}: no token"
            )
        return token, _parse_expiry(minted.get("expires_at"), now)


def resolve_token_provider(
    env_file: Optional[str] = None,
    *,
    environ: Optional[MutableMapping[str, str]] = None,
    http: Optional[HttpJson] = None,
    clock: Callable[[], float] = time.time,
) -> TokenProvider:
    """The configured credential source: App if configured, else static PAT.

    The single seam every caller (CLI, dispatch) resolves credentials
    through. GitHub App configuration wins when present — the PAT keys are
    still popped from the environment for hygiene either way, so a box
    migrating from PAT to App never leaks the leftover PAT to child
    processes.
    """

    credentials = resolve_app_credentials(env_file, environ=environ)
    static = resolve_github_token(env_file, environ=environ)
    if credentials is not None:
        return GitHubAppTokenProvider(credentials, http=http, clock=clock)
    return StaticTokenProvider(static)


__all__ = [
    "APP_ID_KEY",
    "APP_KEY_FILE_KEY",
    "AppCredentials",
    "GitHubAppError",
    "GitHubAppTokenProvider",
    "app_jwt",
    "resolve_app_credentials",
    "resolve_token_provider",
]
