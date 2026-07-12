"""Fetch a remote repository into a local workspace, PAT-authenticated.

``--repo owner/name`` is how a run starts from a repository that is not on
disk yet: the ref is resolved to a clone URL, cloned (or fast-forwarded when
already present), and the resulting directory becomes the run's workspace.

Token hygiene is the whole design:

- The GitHub token comes from an **env file** (``KEY=VALUE`` lines, e.g. the
  same file a systemd unit loads) or, failing that, the process environment.
  It is read into memory and *popped* from ``os.environ`` when found there,
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
from typing import Dict, MutableMapping, Optional

from .errors import DevTeamError
from .execution import CommandResult, CommandRunner

#: Environment (file or process) keys checked for the token, in order.
TOKEN_KEYS = ("GITHUB_TOKEN", "GH_TOKEN")

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


def parse_repo(ref: str) -> RepoRef:
    """Resolve ``owner/name``, an HTTPS/SSH URL, or any git URL to a ref.

    A bare slug means GitHub. Full URLs pass through verbatim (any host,
    including ``file://`` for tests and mirrors); the last two path segments
    name the workspace directory.
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
        path = text.split("://", 1)[1]
        path = path.split("/", 1)[1] if "/" in path else ""
        owner, name = _owner_name_from_path(path, ref)
        return RepoRef(owner=owner, name=name, url=text)
    raise SourceError(
        f"unrecognised repository reference: {ref!r} "
        "(expected owner/name, an https:// URL, or a git@ URL)"
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


def _auth_env(ref: RepoRef, token: Optional[str]) -> Dict[str, str]:
    """Per-command git environment: never prompt; header-based auth.

    The basic-auth header rides in ``GIT_CONFIG_*`` variables scoped to this
    one subprocess — the token stays out of argv and ``.git/config``. Only
    HTTPS transports get the header; a token cannot help SSH or file URLs.
    """

    env = {"GIT_TERMINAL_PROMPT": "0"}
    if token and ref.url.startswith("https://"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
    return env


def _scrub(text: str, token: Optional[str]) -> str:
    if token:
        return text.replace(token, "***")
    return text


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
    env = _auth_env(ref, token)
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
                f"{_scrub(pulled.output, token)}"
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
            f"{_scrub(result.output, token)}"
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
