"""Watch a pull request's CI checks after it is opened (ROADMAP #2).

Delivery used to stop at "PR opened". This is the primitive for the next step:
poll the checks on the delivered PR's head commit until they conclude, and
report the aggregate a human would see on the PR — so a caller can wait for CI
and act on a failure instead of walking away at the open.

Two things are read from the GitHub REST API for the head SHA: the **check-runs**
(the modern GitHub Actions signal) and the legacy **combined commit status** (for
external status checks). A check-run counts as failed on a
``failure``/``cancelled``/``timed_out``/``action_required``/``startup_failure``/``stale``
conclusion, and as pending until it is ``completed``; a combined status of
``failure``/``error`` also fails the watch. The combined status's *pending* value
is ignored — it reads "pending" whenever a repo has no legacy statuses, which is
the normal case for Actions-only repos and would otherwise never clear.

Transport and hygiene mirror :mod:`dev_team.pullrequest`: the HTTP GET is
injectable (so tests never touch the network), the token rides only in the
``Authorization`` header, and it is scrubbed from any error text.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .errors import DevTeamError

#: Injectable transport: ``(url, headers) -> parsed-JSON-response``.
Http = Callable[[str, Mapping[str, str]], Dict]

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_HTTP_TIMEOUT_SECONDS = 30.0

#: Check-run conclusions that count as a failure.
_FAILED_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
)
#: How much of a failed check's own output summary to keep in the digest.
_OUTPUT_CHARS = 500


class ChecksError(DevTeamError):
    """Reading the pull request's checks failed (auth, validation, transport)."""


@dataclass(frozen=True)
class ChecksOutcome:
    """The aggregate state of a PR head's checks at one point in time."""

    state: str  # "success" | "failure" | "pending" | "timeout"
    failed: Sequence[str] = ()
    summary: str = ""

    @property
    def ok(self) -> bool:
        """Whether every check concluded successfully."""

        return self.state == "success"

    @property
    def concluded(self) -> bool:
        """Whether the checks reached a terminal state (success or failure)."""

        return self.state in ("success", "failure")


def _classify(check_runs: Sequence[Dict], combined_state: str) -> ChecksOutcome:
    """Fold the check-runs and combined status into one :class:`ChecksOutcome`.

    Failure wins over pending, and pending over success; when there are no
    check-runs and no legacy failure the state is ``pending`` (the checks may
    not have registered yet — the poll loop's timeout is the backstop).
    """

    failed: List[str] = []
    pending: List[str] = []
    details: List[str] = []
    for run in check_runs:
        name = str(run.get("name") or "check")
        if str(run.get("status") or "") != "completed":
            pending.append(name)
        elif str(run.get("conclusion") or "") in _FAILED_CONCLUSIONS:
            failed.append(name)
            note = str((run.get("output") or {}).get("summary") or "").strip()
            details.append(f"{name}: {note[:_OUTPUT_CHARS]}" if note else name)
    if combined_state in ("failure", "error"):
        failed.append("commit status")
    if failed:
        return ChecksOutcome(
            "failure",
            failed=tuple(failed),
            summary="failed check(s): " + "; ".join(details or failed),
        )
    if pending:
        return ChecksOutcome("pending", summary="waiting on: " + ", ".join(pending))
    # No failures and nothing pending: success once there is a positive signal —
    # a passing check-run, or a definitive combined status for legacy-status-only
    # repos. With neither, the checks have not registered yet: stay pending and
    # let the poll loop's timeout be the backstop.
    if check_runs:
        return ChecksOutcome("success", summary=f"all {len(check_runs)} check(s) passed")
    if combined_state == "success":
        return ChecksOutcome("success", summary="commit status succeeded")
    return ChecksOutcome("pending", summary="waiting on: (no checks reported yet)")


def _http_get(url: str, headers: Mapping[str, str]) -> Dict:
    """GET ``url`` and return the parsed JSON response (default transport)."""

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


@runtime_checkable
class ChecksReader(Protocol):
    """Reads the current checks state for a commit."""

    def status(self, owner: str, name: str, ref: str) -> ChecksOutcome:
        """Return the aggregate checks state for ``ref`` (raises on failure)."""
        ...


@dataclass
class GitHubChecksReader:
    """Reads a commit's checks via the GitHub REST API.

    The token authenticates as ``Authorization: Bearer <token>`` and is never
    placed anywhere else; ``http`` is injectable for testing.
    """

    token: str = field(repr=False)  # never let a repr/traceback/config dump print it
    api_base: str = _API_BASE
    http: Optional[Http] = field(default=None, repr=False)  # a bound fake may capture secrets

    def status(self, owner: str, name: str, ref: str) -> ChecksOutcome:
        runs = self._get(f"/repos/{owner}/{name}/commits/{ref}/check-runs")
        combined = self._get(f"/repos/{owner}/{name}/commits/{ref}/status")
        check_runs = runs.get("check_runs")
        return _classify(
            check_runs if isinstance(check_runs, list) else [],
            str(combined.get("state") or ""),
        )

    def _get(self, path: str) -> Dict:
        url = f"{self.api_base}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        http = self.http or _http_get
        try:
            response = http(url, headers)
        except urllib.error.HTTPError as exc:
            raise ChecksError(self._describe(exc)) from exc
        except urllib.error.URLError as exc:
            raise ChecksError(
                self._scrub(f"could not reach {self.api_base}: {exc.reason}")
            ) from exc
        if not isinstance(response, dict):
            raise ChecksError("unexpected response from the GitHub checks API")
        return response

    def _describe(self, exc: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("message") or "")
        except (ValueError, OSError):
            detail = ""
        if exc.code in (401, 403):
            detail = detail or "authentication failed — check the token's repo read scope"
        return self._scrub(
            f"GitHub returned {exc.code} reading the checks: {detail}".rstrip(": ")
        )

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text


def watch_checks(
    reader: ChecksReader,
    owner: str,
    name: str,
    ref: str,
    *,
    max_polls: int = 30,
    poll_interval_seconds: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ChecksOutcome:
    """Poll ``reader`` for ``ref``'s checks until they conclude or polls run out.

    Returns as soon as the checks reach a terminal state (success or failure);
    otherwise, after ``max_polls`` polls (spaced by ``poll_interval_seconds``,
    the first with no wait), returns a ``timeout`` outcome carrying the last
    pending detail. ``sleep`` is injected so tests never actually wait.
    """

    last = ChecksOutcome("pending")
    for attempt in range(max_polls):
        if attempt:
            sleep(poll_interval_seconds)
        last = reader.status(owner, name, ref)
        if last.concluded:
            return last
    return ChecksOutcome(
        "timeout",
        failed=last.failed,
        summary=f"checks did not conclude within {max_polls} poll(s); last: {last.summary}",
    )
