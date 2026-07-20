"""Fetch a remote repository into a local workspace, PAT-authenticated.

``--repo owner/name`` is how a run starts from a repository that is not on
disk yet: the ref is resolved to a clone URL, cloned (or fast-forwarded when
already present), and the resulting directory becomes the run's workspace.

Token hygiene is the whole design:

- The GitHub token comes from an **env file** (``KEY=VALUE`` lines, e.g. the
  same file a systemd unit loads) or, failing that, the process environment.
  The file is found without being passed: ``--env-file`` overrides a default
  search of ``./.env``, ``~/.config/dev-team/dev-team.env``, and
  ``/etc/dev-team/dev-team.env`` — configure it once at setup time. Tokens
  are read into memory and *popped* from ``os.environ`` when found there,
  so commands the engines later execute — gates, build probes, the code
  under audit — can never read it.
- git receives the credential through per-command ``GIT_CONFIG_*``
  environment variables (an ``http.extraheader`` basic-auth header), never
  through the URL: nothing token-shaped lands in argv, ``.git/config``, or
  process listings.
- Any command output that could echo the token is scrubbed before it
  reaches an exception message.

A fine-grained PAT with read-only **Contents** permission is all a private
clone needs; prefer that over a classic ``repo``-scope token.
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, MutableMapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit

from .errors import DevTeamError
from .execution import CommandResult, CommandRunner

#: Environment (file or process) keys checked for the token, in order.
TOKEN_KEYS = ("GITHUB_TOKEN", "GH_TOKEN")

#: System-wide env file, the last resort of the default search.
SYSTEM_ENV_FILE = "/etc/dev-team/dev-team.env"


def default_env_file(
    candidates: Optional[Sequence[Path]] = None,
) -> Optional[str]:
    """The env file a run should use when ``--env-file`` was not passed.

    Searched in order — project, then user, then system — so the credential
    is configured once at setup time and every later run just finds it:

    1. ``./.env`` (the working directory)
    2. ``$XDG_CONFIG_HOME/dev-team/dev-team.env``
       (``~/.config/dev-team/dev-team.env`` by default)
    3. ``/etc/dev-team/dev-team.env``

    Returns the first path that exists, or ``None``.
    """

    if candidates is None:
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", "~/.config")
        ).expanduser()
        candidates = (
            Path(".env"),
            config_home / "dev-team" / "dev-team.env",
            Path(SYSTEM_ENV_FILE),
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None

#: Ceiling for one git command; large monoliths clone slowly, not forever.
_GIT_TIMEOUT_SECONDS = 900.0

_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class SourceError(DevTeamError):
    """Fetching the repository failed (parse, clone, or update)."""


@dataclass(frozen=True)
class RepoRef:
    """A repository reference resolved to something git can clone."""

    owner: str
    name: str
    url: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def workspace_name(self) -> str:
        """A filesystem-safe directory name for the clone."""

        return f"{self.owner}__{self.name}"


#: URL schemes a ``://`` reference may use. Only these transports are safe to
#: hand to ``git``: ``file://`` (a local mirror) and git's RCE-capable
#: ``ext::``/``fd::`` helper transports are excluded, so a scheme reachable
#: from the authenticated dispatch API can never turn a repo reference into
#: local file access or arbitrary command execution. ``file://`` is re-admitted
#: only when a trusted caller opts in with ``allow_local=True`` (tests and
#: local mirrors), never on the default authenticated path.
_ALLOWED_URL_SCHEMES = frozenset({"https", "ssh", "git"})


def parse_repo(ref: str, *, allow_local: bool = False) -> RepoRef:
    """Resolve ``owner/name``, an HTTPS/SSH URL, or any git URL to a ref.

    A bare slug means GitHub. A ``scheme://`` URL passes through verbatim (any
    host) only for the transports in :data:`_ALLOWED_URL_SCHEMES`; the last two
    path segments name the workspace directory. ``file://`` is accepted only
    when ``allow_local=True`` (a trusted local caller — never the authenticated
    dispatch path), and RCE-capable helper transports such as ``ext::`` are
    always refused. The ``git@host:path`` shorthand is unaffected.
    """

    text = ref.strip()
    if _SLUG_RE.match(text):
        owner, name = text.split("/")
        name = _strip_git_suffix(name)
        return RepoRef(owner=owner, name=name, url=f"https://github.com/{owner}/{name}.git")
    if text.startswith("git@") and ":" in text:
        path = text.split(":", 1)[1]
        owner, name = _owner_name_from_path(path, ref)
        return RepoRef(owner=owner, name=name, url=text)
    if "://" in text:
        _reject_embedded_credentials(text)
        scheme = urlsplit(text).scheme
        allowed = _ALLOWED_URL_SCHEMES | {"file"} if allow_local else _ALLOWED_URL_SCHEMES
        if scheme not in allowed:
            raise SourceError(f"unsupported URL scheme {scheme!r}")
        path = text.split("://", 1)[1]
        path = path.split("/", 1)[1] if "/" in path else ""
        owner, name = _owner_name_from_path(path, ref)
        return RepoRef(owner=owner, name=name, url=text)
    raise SourceError(
        f"unrecognised repository reference: {ref!r} "
        "(expected owner/name, an https:// URL, or a git@ URL)"
    )


def _reject_embedded_credentials(ref: str) -> None:
    """Refuse a URL that carries credentials in its authority component.

    A ``https://user:pass@host/...`` (or bare ``https://token@host/...``) URL
    would put the secret straight into git's argv and ``.git/config``, where
    neither the header-based auth design nor :func:`scrub_credentials` can
    reach it — so
    it must be rejected outright rather than silently propagated. A bare
    ``ssh://git@host`` username is left alone: that is a transport user, not a
    secret. Credentials belong in an env file, so point the user at
    ``--env-file``.
    """

    parts = urlsplit(ref)
    has_secret = parts.password is not None or (
        parts.username is not None and parts.scheme in ("http", "https")
    )
    if has_secret:
        raise SourceError(
            f"refusing a URL with embedded credentials: {ref!r}; "
            "put the token in --env-file (or ./.env), not in the URL"
        )


def _strip_git_suffix(name: str) -> str:
    return name[:-4] if name.endswith(".git") else name


def _owner_name_from_path(path: str, ref: str) -> tuple:
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        raise SourceError(
            f"cannot derive owner/name from repository reference: {ref!r}"
        )
    return segments[-2], _strip_git_suffix(segments[-1])


def load_env_file(path: str) -> Dict[str, str]:
    """Parse a ``KEY=VALUE`` env file (systemd/.env style).

    Blank lines and ``#`` comments are skipped, an optional ``export``
    prefix and surrounding single/double quotes are tolerated. Values are
    returned, never exported into the process environment.
    """

    file = Path(path)
    if not file.is_file():
        raise SourceError(f"env file not found: {path}")
    values: Dict[str, str] = {}
    for line in file.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_github_token(
    env_file: Optional[str] = None,
    *,
    environ: Optional[MutableMapping[str, str]] = None,
) -> Optional[str]:
    """The GitHub token to clone with, or ``None`` for anonymous access.

    An env file wins over the process environment. Tokens found in the
    process environment are **always removed** from it — even when the env
    file supplies the credential — so nothing the engines later execute
    (gates, build probes, delivered code) inherits them.
    """

    live = os.environ if environ is None else environ
    inherited: Optional[str] = None
    for key in TOKEN_KEYS:
        value = live.pop(key, None)
        if value and inherited is None:
            inherited = value
    if env_file is not None:
        values = load_env_file(env_file)
        for key in TOKEN_KEYS:
            if values.get(key):
                return values[key]
    return inherited


class TokenProvider(Protocol):
    """The seam every GitHub credential flows through.

    ``token_for`` returns the credential to present for ``ref`` **right
    now** — call it at each use rather than caching the result, so a
    provider backed by short-lived credentials (a GitHub App installation
    token) can re-mint under a long run while the static-PAT provider keeps
    returning the same value.
    """

    def token_for(self, ref: "RepoRef") -> Optional[str]: ...


@dataclass(frozen=True)
class StaticTokenProvider:
    """The classic model: one PAT (or nothing) for every repository."""

    token: Optional[str] = None

    def token_for(self, ref: "RepoRef") -> Optional[str]:
        return self.token


#: Hosts a GitHub PAT may be sent to. The token is scoped to GitHub, so the
#: ``Authorization`` header is attached only for these hosts — never for an
#: arbitrary ``--repo https://other-host/...``, which would leak the token to
#: whoever runs that host. git simply falls back to anonymous access there.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "api.github.com"})


def _is_github_https(url: str) -> bool:
    """Whether ``url`` is an HTTPS URL served by a GitHub host.

    The host is parsed with :func:`urllib.parse.urlsplit` (so any userinfo or
    ``:port`` is ignored) and compared against :data:`_GITHUB_HOSTS`;
    ``hostname`` is already lower-cased, so the match is case-insensitive.
    """

    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    return (parts.hostname or "") in _GITHUB_HOSTS


def git_auth_env(ref: RepoRef, token: Optional[str]) -> Dict[str, str]:
    """Per-command git environment: never prompt; header-based auth.

    The basic-auth header rides in ``GIT_CONFIG_*`` variables scoped to this
    one subprocess — the token stays out of argv and ``.git/config``. The
    header is attached only for GitHub HTTPS remotes: a token cannot help SSH
    or file URLs, and sending it to a non-GitHub HTTPS host would hand the
    credential to an arbitrary third party.

    Public because the same env authenticates any git operation against the
    remote, not just the clone — the delivery target reuses it to *push* the
    delivered branch (paired with :func:`scrub_credentials`, exactly as
    :func:`clone_or_update` pairs them here).
    """

    env = {"GIT_TERMINAL_PROMPT": "0"}
    if token and _is_github_https(ref.url):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
    return env


def scrub_credentials(text: str, token: Optional[str]) -> str:
    """Redact the token *and* the basic-auth header value derived from it.

    A verbose/``GIT_TRACE`` line can echo the ``AUTHORIZATION: basic <base64>``
    header rather than the raw token, so the exact base64 value
    :func:`git_auth_env` would compute is redacted alongside the token itself.

    Public for the same reason as :func:`git_auth_env`: any git call that
    carries the header can leak it into its output, so every caller (the clone
    here, the delivery target's push) redacts with this before raising.
    """

    if not token:
        return text
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return text.replace(token, "***").replace(basic, "***")


def clone_or_update(
    ref: RepoRef,
    dest: str,
    *,
    runner: CommandRunner,
    token: Optional[str] = None,
    timeout: float = _GIT_TIMEOUT_SECONDS,
) -> str:
    """Ensure ``dest`` holds a current clone of ``ref``; return ``dest``.

    A fresh directory is cloned; an existing clone of the *same* remote is
    fast-forwarded (local work makes that fail loudly rather than being
    overwritten); anything else at ``dest`` is refused.
    """

    destination = Path(dest)
    env = git_auth_env(ref, token)
    if (destination / ".git").exists():
        remote = runner.run(
            ["git", "remote", "get-url", "origin"], cwd=dest, timeout=timeout
        )
        if remote.output != ref.url:
            raise SourceError(
                f"{dest} already holds a clone of {remote.output or 'an unknown remote'}, "
                f"not {ref.url}; pass a different --workspace"
            )
        pulled = runner.run(
            ["git", "pull", "--ff-only"], cwd=dest, timeout=timeout, env=env
        )
        if not pulled.ok:
            raise SourceError(
                f"could not update the existing clone at {dest} "
                f"(local changes or a diverged branch?): "
                f"{scrub_credentials(pulled.output, token)}"
            )
        return dest
    if destination.exists() and any(destination.iterdir()):
        raise SourceError(f"{dest} exists and is not a git repository")
    result: CommandResult = runner.run(
        ["git", "clone", ref.url, dest], timeout=timeout, env=env
    )
    if not result.ok:
        raise SourceError(
            f"cloning {ref.slug} failed{_clone_hint(result.output)}: "
            f"{scrub_credentials(result.output, token)}"
        )
    return dest


def _clone_hint(output: str) -> str:
    """Translate git's failure into what actually went wrong on GitHub."""

    lowered = output.lower()
    if "could not read username" in lowered or "authentication failed" in lowered:
        return (
            " (no usable credential: the repository does not exist, or is "
            "private and no GITHUB_TOKEN was found — put one in --env-file "
            "or ./.env)"
        )
    if "404" in output or "not found" in lowered:
        return (
            " (GitHub answers 404/not-found for private repositories the "
            "token cannot read — check the token's access as well as the name)"
        )
    return ""
