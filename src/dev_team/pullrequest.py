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

:class:`GitHubCheckRunsClient` extends the same file with the other half of
ROADMAP #2's remaining work: watching the opened PR's required checks (the
GitHub Checks API, since this repo's own CI reports via Actions check runs,
not the legacy combined-status endpoint). It is opt-in, CLI-only, and never
feeds a result back into the delivery task loop — see ``--watch-checks`` in
``cli.py`` and issue #71.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from .errors import DevTeamError

#: Injectable transport: ``(url, body, headers) -> parsed-JSON-response``.
#: The default is :func:`_http_post`; tests pass a recording fake instead.
Http = Callable[[str, bytes, Mapping[str, str]], Dict]

#: Injectable transport for the check-runs GET: ``(url, headers) -> parsed-JSON``.
#: The default is :func:`_http_get`; tests pass a recording fake instead.
HttpGet = Callable[[str, Mapping[str, str]], Dict]

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_HTTP_TIMEOUT_SECONDS = 30.0

#: Hard ceilings on :meth:`GitHubCheckRunsClient.watch`'s timeout/poll-interval,
#: enforced in code before any polling begins (BPG §4 resource-bounding): a
#: misconfigured caller cannot wedge the calling process indefinitely.
MAX_CHECKS_TIMEOUT_SECONDS = 900.0
MIN_CHECKS_POLL_INTERVAL_SECONDS = 5.0

#: Check-run conclusions that count as a pass; anything else completed is a
#: failure. A closed enum with an explicit default — an unrecognised
#: conclusion string from GitHub is never trusted as success (fail-secure).
_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


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


def aggregate_check_runs(check_runs: Sequence[Mapping]) -> str:
    """Reduce a list of GitHub check-run objects to one aggregate state.

    ``pending`` if any run's ``status`` isn't ``"completed"``; ``success`` if
    every completed run's ``conclusion`` is in the success set; ``failure`` for
    any other completed conclusion (``failure``/``cancelled``/``timed_out``/
    ``action_required``/``stale``/anything unrecognised); ``no_checks`` for an
    empty list (CI hasn't indexed the SHA yet, or the repo has none
    configured). Untrusted API strings are matched against a closed enum with
    a safe default — an unfamiliar ``conclusion`` never resolves to success.
    """

    if not check_runs:
        return "no_checks"
    if any(run.get("status") != "completed" for run in check_runs):
        return "pending"
    if any(run.get("conclusion") not in _SUCCESS_CONCLUSIONS for run in check_runs):
        return "failure"
    return "success"


def _clamp_timeout_seconds(timeout_seconds: float) -> float:
    """Clamp to the hard ceiling — never honoured literally (BPG §4)."""

    return min(max(timeout_seconds, 0.0), MAX_CHECKS_TIMEOUT_SECONDS)


def _clamp_poll_interval_seconds(poll_interval_seconds: float) -> float:
    """Floor to the hard minimum — never honoured literally (BPG §4)."""

    return max(poll_interval_seconds, MIN_CHECKS_POLL_INTERVAL_SECONDS)


@dataclass(frozen=True)
class CheckRunsResult:
    """The aggregated outcome of a (possibly bounded) check-runs watch."""

    state: str  # pending | success | failure | no_checks | unknown
    check_runs: List[Dict] = field(default_factory=list)
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def failing_names(self) -> List[str]:
        """Names of completed runs whose conclusion was not a success one."""

        return [
            str(run.get("name") or "")
            for run in self.check_runs
            if run.get("status") == "completed"
            and run.get("conclusion") not in _SUCCESS_CONCLUSIONS
        ]

    def to_dict(self) -> Dict:
        return {
            "state": self.state,
            "failing_checks": self.failing_names,
            "timed_out": self.timed_out,
            "error": self.error,
        }


def _http_get(url: str, headers: Mapping[str, str]) -> Dict:
    """GET ``url`` and return the parsed JSON response (default transport)."""

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass
class GitHubCheckRunsClient:
    """Watches a commit's GitHub check runs via the Checks API.

    Mirrors :class:`GitHubPullRequestPublisher`'s injectable-``http``/
    token-scrub conventions, plus injectable ``sleep``/``clock`` (mirroring
    :class:`dev_team.verification.RemoteCIGate`'s clock-injection pattern) so
    tests never perform a real wait. :meth:`watch` clamps its
    ``timeout_seconds``/``poll_interval_seconds`` to a hard ceiling/floor
    before polling begins, and never raises: a transport/auth failure is
    caught and surfaced as ``state="unknown"`` (fail-secure) rather than
    propagated — the PR is already open and real, so a failed watch must
    never flip the caller's success/exit code.
    """

    token: str = field(repr=False)  # never let a repr/traceback/config dump print it
    api_base: str = _API_BASE
    http: Optional[HttpGet] = field(default=None, repr=False)  # a bound fake may capture secrets
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def watch(
        self,
        owner: str,
        name: str,
        ref: str,
        *,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 10.0,
    ) -> CheckRunsResult:
        timeout_seconds = _clamp_timeout_seconds(timeout_seconds)
        poll_interval_seconds = _clamp_poll_interval_seconds(poll_interval_seconds)
        url = f"{self.api_base}/repos/{owner}/{name}/commits/{ref}/check-runs"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        http = self.http or _http_get
        start = self.clock()
        check_runs: List[Dict] = []
        while True:
            try:
                response = http(url, headers)
                check_runs = list(response.get("check_runs") or [])
            except urllib.error.HTTPError as exc:
                return CheckRunsResult(state="unknown", error=self._describe(exc))
            except urllib.error.URLError as exc:
                return CheckRunsResult(
                    state="unknown",
                    error=self._scrub(f"could not reach {self.api_base}: {exc.reason}"),
                )
            state = aggregate_check_runs(check_runs)
            if state != "pending":
                return CheckRunsResult(state=state, check_runs=check_runs)
            if self.clock() - start >= timeout_seconds:
                return CheckRunsResult(state="pending", check_runs=check_runs, timed_out=True)
            self.sleep(poll_interval_seconds)

    def _describe(self, exc: urllib.error.HTTPError) -> str:
        """A helpful, token-free message from a GitHub error response."""

        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("message") or "")
        except (ValueError, OSError):
            detail = ""
        if exc.code in (401, 403):
            detail = detail or "authentication failed — check the token's repo scope"
        elif exc.code == 404:
            detail = detail or "no check runs found (unknown ref, or none configured yet)"
        return self._scrub(
            f"GitHub returned {exc.code} fetching check runs: {detail}".rstrip(": ")
        )

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
