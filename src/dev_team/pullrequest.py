"""Open a pull request for a delivered branch (the delivery terminus).

A dev team's real interface is a pull request reviewed by humans and CI, not a
local commit (ROADMAP item 2). This module is the primitive for that step: given
a branch that has been pushed to GitHub, open a PR whose body is the delivery's
own outcome report.

Like :mod:`dev_team.depscan`'s OSV client, the network call is injectable — the
publisher takes an ``http`` callable, defaulting to a small :mod:`urllib` POST —
so the whole thing is unit-testable without touching the network, and a fake
(:class:`FakePullRequestPublisher`) backs the higher layers' tests. Token hygiene
mirrors :mod:`dev_team.sources`: the credential rides only in the ``Authorization``
header, never in a URL/argv/log, and is scrubbed from any error text.

Wiring this into the delivery engine (push the ``dev-team/<feature>`` branch,
then open the PR after security approves the commit) is the follow-up; this PR
lands the tested primitive.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Protocol, runtime_checkable

from .errors import DevTeamError

#: Injectable transport: ``(url, body, headers) -> parsed-JSON-response``.
#: The default is :func:`_http_post`; tests pass a recording fake instead.
Http = Callable[[str, bytes, Mapping[str, str]], Dict]

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_HTTP_TIMEOUT_SECONDS = 30.0


class PullRequestError(DevTeamError):
    """Opening the pull request failed (auth, validation, or transport)."""


@dataclass(frozen=True)
class PullRequestRequest:
    """What to open: the delivered branch, its base, and the report body."""

    owner: str
    name: str
    title: str
    body: str
    head: str  # the delivered branch that was pushed, e.g. dev-team/<feature>
    base: str = "main"
    draft: bool = False


@dataclass(frozen=True)
class PullRequest:
    """An opened pull request."""

    number: int
    url: str  # the human-facing html_url


@runtime_checkable
class PullRequestPublisher(Protocol):
    """Opens a pull request and returns it."""

    def open(self, request: PullRequestRequest) -> PullRequest:
        """Open the PR described by ``request`` (raises on failure)."""
        ...


def _http_post(url: str, body: bytes, headers: Mapping[str, str]) -> Dict:
    """POST ``body`` to ``url`` and return the parsed JSON response (default)."""

    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass
class GitHubPullRequestPublisher:
    """Opens a pull request via the GitHub REST API.

    The token authenticates the call as ``Authorization: Bearer <token>`` and is
    never placed anywhere else; ``http`` is injectable for testing.
    """

    token: str = field(repr=False)  # never let a repr/traceback/config dump print it
    api_base: str = _API_BASE
    http: Optional[Http] = field(default=None, repr=False)  # a bound fake may capture secrets

    def open(self, request: PullRequestRequest) -> PullRequest:
        url = f"{self.api_base}/repos/{request.owner}/{request.name}/pulls"
        payload = {
            "title": request.title,
            "body": request.body,
            "head": request.head,
            "base": request.base,
            "draft": request.draft,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        http = self.http or _http_post
        try:
            response = http(url, json.dumps(payload).encode("utf-8"), headers)
        except urllib.error.HTTPError as exc:
            raise PullRequestError(self._describe(exc)) from exc
        except urllib.error.URLError as exc:
            raise PullRequestError(
                self._scrub(f"could not reach {self.api_base}: {exc.reason}")
            ) from exc
        try:
            return PullRequest(number=int(response["number"]), url=str(response["html_url"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PullRequestError(
                "unexpected response from the GitHub pulls API (no number/html_url)"
            ) from exc

    def _describe(self, exc: urllib.error.HTTPError) -> str:
        """A helpful, token-free message from a GitHub error response."""

        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("message") or "")
        except (ValueError, OSError):
            detail = ""
        if exc.code == 422 and "already exist" in detail.lower():
            detail = f"{detail} (a pull request for this branch may already be open)"
        elif exc.code in (401, 403):
            detail = detail or "authentication failed — check the token's pull-request scope"
        return self._scrub(f"GitHub returned {exc.code} opening the pull request: {detail}".rstrip(": "))

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text


@dataclass
class FakePullRequestPublisher:
    """A scripted :class:`PullRequestPublisher` for tests.

    Records every request; returns ``result`` (or raises ``error`` if set).
    """

    result: PullRequest = field(default_factory=lambda: PullRequest(1, "https://example/pr/1"))
    error: Optional[Exception] = None
    requests: List[PullRequestRequest] = field(default_factory=list)

    def open(self, request: PullRequestRequest) -> PullRequest:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result
