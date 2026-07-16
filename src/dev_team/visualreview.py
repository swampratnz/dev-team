"""Visual review: look at the rendered UI, not just the tests (ROADMAP option B).

The delivery pipeline gates on correctness (tests) and safety (security), and
the greenfield visual baseline (:mod:`dev_team.frontend`) nudges the engineer
toward sane defaults — but nothing in the loop ever *sees* a rendered page. This
module is the skeleton of a review that does: serve the delivered app,
screenshot its pages, and critique the screenshots with a vision model.

It is deliberately split into three seams — :class:`AppServer` (serve the app),
:class:`PageCapturer` (screenshot its routes), :class:`VisualReviewer` (critique
the screenshots) — each a Protocol with an in-memory Fake, so the whole flow is
exercised with **no browser and no network**. The real Playwright/vision
implementations arrive behind these seams later; they are the only
non-deterministic parts and never run in the core test suite.

The review is **advisory**: it produces a :class:`VisualReport` attached to the
delivery outcome, and never blocks the commit. Non-deterministic gates are
dangerous (a flaky render would randomly fail a build), so visual findings
inform rather than gate.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from .models import Severity
from .policy import SideEffectPolicy
from .sdk import AgentResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .budget import Budget

# The critique rubric. Mirrors the frontend design baseline's criteria, framed
# for judging a rendered screenshot rather than authoring code.
VISUAL_RUBRIC = """\
Judge each screenshot of a rendered web page. Report concrete, visible problems
only (not code style). Look for:
- Unstyled browser defaults (Times New Roman body text, default blue links,
  raw bullet lists) — a page that looks like plain HTML with no CSS.
- Inconsistent or cramped spacing; elements touching edges or each other.
- Poor contrast or unreadable text; text running the full window width.
- Broken or overflowing layout; horizontal scrollbars; elements off-screen.
- Missing visual hierarchy (headings indistinguishable from body text).
- Misaligned or ragged elements that should line up.
Report nothing when a page looks clean and intentional."""


@dataclass(frozen=True)
class Screenshot:
    """A captured page: the route and its PNG bytes."""

    route: str
    png: bytes


@dataclass(frozen=True)
class VisualFinding:
    """One visible problem the reviewer spotted on a page."""

    route: str
    issue: str
    severity: Severity = Severity.MINOR


@dataclass
class VisualReport:
    """The outcome of a visual review (advisory — never blocks the commit)."""

    findings: List[VisualFinding] = field(default_factory=list)
    summary: str = ""
    routes: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Whether nothing major/critical was found (purely informational)."""

        return not any(
            f.severity in (Severity.MAJOR, Severity.CRITICAL) for f in self.findings
        )


@runtime_checkable
class AppServer(Protocol):
    """Serves the delivered app, yielding its base URL for a ``with`` block."""

    def serve(self) -> AbstractContextManager[str]:
        """Context manager yielding the base URL of the running app."""
        ...


@runtime_checkable
class PageCapturer(Protocol):
    """Screenshots each route of a running app."""

    def capture(self, base_url: str, routes: Sequence[str]) -> List[Screenshot]:
        """Return one :class:`Screenshot` per route under ``base_url``."""
        ...


@runtime_checkable
class VisualReviewer(Protocol):
    """Critiques screenshots against a rubric with a vision model."""

    async def critique(
        self, screenshots: Sequence[Screenshot], rubric: str
    ) -> VisualReport:
        """Return a :class:`VisualReport` for ``screenshots``."""
        ...


@dataclass
class FakeAppServer:
    """An :class:`AppServer` that yields a canned base URL without a subprocess."""

    base_url: str = "http://127.0.0.1:0"
    starts: int = 0

    @contextmanager
    def serve(self) -> Iterator[str]:
        self.starts += 1
        yield self.base_url


@dataclass
class FakePageCapturer:
    """A :class:`PageCapturer` returning canned screenshots (no browser)."""

    screenshots: Optional[List[Screenshot]] = None
    calls: List[tuple] = field(default_factory=list)

    def capture(self, base_url: str, routes: Sequence[str]) -> List[Screenshot]:
        self.calls.append((base_url, tuple(routes)))
        if self.screenshots is not None:
            return list(self.screenshots)
        return [Screenshot(route=r, png=b"\x89PNG\r\n-fake") for r in routes]


@dataclass
class FakeVisualReviewer:
    """A :class:`VisualReviewer` returning a canned report (no vision call)."""

    report: Optional[VisualReport] = None
    seen: List[Screenshot] = field(default_factory=list)

    async def critique(
        self, screenshots: Sequence[Screenshot], rubric: str
    ) -> VisualReport:
        self.seen.extend(screenshots)
        if self.report is not None:
            return self.report
        return VisualReport(
            findings=[],
            summary="no visible problems",
            routes=[s.route for s in screenshots],
        )


# --------------------------------------------------------------------------- #
# Real implementations of the three seams.
#
# These carry the only non-deterministic, dependency-heavy parts of the flow: a
# subprocess dev-server, a headless browser (Playwright), and a vision model
# call (the ``anthropic`` API SDK). Both extra dependencies are optional — they
# live behind the ``dev-team[visual]`` extra and are imported lazily inside the
# real-I/O methods, so importing this module (and the package) never requires
# them and the core test suite never touches them. The irreducible I/O leaves
# are marked ``# pragma: no cover`` (exercised only in CI / real runs, like
# :mod:`dev_team.benchmark`); every pure decision around them — command
# templating, URL joining, response parsing, cost metering — is a module-level
# helper covered by the deterministic suite.
# --------------------------------------------------------------------------- #

_PORT_PLACEHOLDER = "{port}"


def _render_command(command: Sequence[str], port: int) -> List[str]:
    """Substitute ``{port}`` with ``port`` in every token of ``command``."""

    return [str(token).replace(_PORT_PLACEHOLDER, str(port)) for token in command]


def _join_url(base_url: str, route: str) -> str:
    """Join ``base_url`` and ``route``; an absolute ``route`` is returned as-is."""

    if route.startswith(("http://", "https://")):
        return route
    return base_url.rstrip("/") + "/" + route.lstrip("/")


@dataclass
class SubprocessAppServer:
    """An :class:`AppServer` that runs the app as a subprocess on a free port.

    The *serve contract* is explicit and deterministic: ``serve_command`` is a
    template that must contain a ``{port}`` placeholder, substituted with a
    free port at serve time (guessing a port from the process's stdout would be
    fragile). The command is checked against a :class:`SideEffectPolicy` at
    construction — a catastrophically unsafe serve command (``rm -rf``, ``sudo``,
    ``mkfs``, a fork bomb) is refused up front rather than launched — and the
    ``{port}`` requirement is enforced there too, so a misconfiguration fails
    fast instead of mid-delivery.

    This is defence-in-depth, not a sandbox: the server runs as a bare
    subprocess. For untrusted or unattended runs, put the whole workspace inside
    an isolated container as well (see :mod:`dev_team.policy`).
    """

    serve_command: Sequence[str]
    cwd: Optional[str] = None
    host: str = "127.0.0.1"
    readiness_path: str = "/"
    startup_timeout: float = 30.0
    poll_interval: float = 0.25
    policy: SideEffectPolicy = field(default_factory=SideEffectPolicy)
    env: Optional[Mapping[str, str]] = None

    def __post_init__(self) -> None:
        if not any(_PORT_PLACEHOLDER in str(token) for token in self.serve_command):
            raise ValueError(
                "serve_command must contain a '{port}' placeholder so the app "
                "can be started on a free port (e.g. 'npm run preview -- --port {port}')"
            )
        verdict = self.policy.evaluate(list(self.serve_command))
        if not verdict.allowed:
            raise ValueError(f"serve_command rejected by policy: {verdict.reason}")

    @contextmanager
    def serve(self) -> Iterator[str]:  # pragma: no cover - real subprocess/socket, CI-only
        port = _free_port(self.host)
        argv = _render_command(self.serve_command, port)
        process = subprocess.Popen(
            argv,
            cwd=self.cwd,
            env=dict(self.env) if self.env is not None else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://{self.host}:{port}"
        try:
            _await_ready(
                base_url + self.readiness_path,
                process,
                self.startup_timeout,
                self.poll_interval,
            )
            yield base_url
        finally:
            _terminate(process)


def _free_port(host: str) -> int:  # pragma: no cover - real socket, CI-only
    """Bind an ephemeral port, then release it for the child to reuse."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _await_ready(
    url: str, process: "subprocess.Popen", timeout: float, interval: float
) -> None:  # pragma: no cover - real network/timing, CI-only
    """Poll ``url`` until it answers, the process dies, or ``timeout`` elapses."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"serve process exited early (code {process.returncode}) before "
                f"{url} was ready"
            )
        try:
            with urllib.request.urlopen(url, timeout=interval):  # noqa: S310 - localhost
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(interval)
    raise TimeoutError(f"app did not become ready at {url} within {timeout:.0f}s")


def _terminate(process: "subprocess.Popen") -> None:  # pragma: no cover - real subprocess, CI-only
    """Stop the serve process, escalating to kill if it will not exit."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


@dataclass
class PlaywrightPageCapturer:
    """A :class:`PageCapturer` that screenshots routes with headless Chromium.

    Uses Playwright's sync API (lazily imported from the optional ``visual``
    extra). In this environment Chromium is pre-installed, so no browser
    download is triggered.
    """

    viewport_width: int = 1280
    viewport_height: int = 800
    wait_until: str = "networkidle"
    timeout_ms: float = 15000.0
    full_page: bool = True

    def capture(
        self, base_url: str, routes: Sequence[str]
    ) -> List[Screenshot]:  # pragma: no cover - real browser, CI-only
        from playwright.sync_api import sync_playwright

        shots: List[Screenshot] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": self.viewport_width, "height": self.viewport_height}
                )
                for route in routes:
                    page.goto(
                        _join_url(base_url, route),
                        wait_until=self.wait_until,
                        timeout=self.timeout_ms,
                    )
                    shots.append(
                        Screenshot(route=route, png=page.screenshot(full_page=self.full_page))
                    )
            finally:
                browser.close()
        return shots


# Vision-critique response contract. The model answers with a single JSON
# object; we parse it tolerantly (see ``_parse_json_object``) so a malformed or
# chatty reply degrades to "no findings" rather than crashing the advisory run.
_RESPONSE_INSTRUCTIONS = (
    'Respond with a single JSON object and nothing else: {"summary": "<one '
    'sentence overall>", "findings": [{"route": "<the route>", "issue": "<the '
    'visible problem>", "severity": "minor|major|critical"}]}. Use an empty '
    '"findings" list when every page looks clean and intentional.'
)

# USD per 1M tokens (input, output), from the published Claude price list. An
# unknown model is metered at the Opus tier rather than as free.
_PRICE_PER_MTOK = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE_PER_MTOK = (5.0, 25.0)

_SEVERITY_BY_NAME = {severity.value: severity for severity in Severity}


def _severity(value: Any) -> Severity:
    """Map a model-supplied severity string to a :class:`Severity` (default minor)."""

    if isinstance(value, str):
        return _SEVERITY_BY_NAME.get(value.strip().lower(), Severity.MINOR)
    return Severity.MINOR


def _usage_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Cost in USD for a vision call of the given token usage under ``model``."""

    price_in, price_out = _PRICE_PER_MTOK.get(model, _DEFAULT_PRICE_PER_MTOK)
    tokens_in = max(0, int(input_tokens))
    tokens_out = max(0, int(output_tokens))
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def _build_content(screenshots: Sequence[Screenshot], rubric: str) -> List[dict]:
    """Assemble the multimodal message content: rubric text then each image.

    Each screenshot is preceded by a text block naming its route so the model
    can attribute findings, and carried as a base64 PNG image block.
    """

    content: List[dict] = [{"type": "text", "text": f"{rubric}\n\n{_RESPONSE_INSTRUCTIONS}"}]
    for shot in screenshots:
        content.append({"type": "text", "text": f"Route: {shot.route}"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(shot.png).decode("ascii"),
                },
            }
        )
    return content


def _parse_json_object(text: str) -> dict:
    """Extract the outermost ``{...}`` JSON object from ``text`` (``{}`` on failure).

    Tolerant by design: the model may wrap the object in a markdown fence or a
    sentence of prose, and a visual review must never crash on a chatty reply.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end < start:
        return {}
    try:
        # The slice begins with '{' and ends with '}', so any value json.loads
        # accepts here is necessarily an object (dict).
        return json.loads(text[start : end + 1])
    except ValueError:
        return {}


def _payload_from_response(response: Any) -> dict:
    """Concatenate the response's text blocks and parse the JSON object within."""

    text = "".join(getattr(block, "text", "") for block in response.content)
    return _parse_json_object(text)


def _report_from_payload(payload: dict, routes: Sequence[str]) -> VisualReport:
    """Map a parsed critique payload into a :class:`VisualReport`.

    The ``issue``/``summary`` strings are model output derived from reading a
    screenshot, so they are *untrusted*: a rendered page could contain injected
    text. They are kept strictly as data here (surfaced in the report and event
    journal only). Any consumer that feeds them back into a prompt or renders
    them as markup must fence/escape them first.
    """

    findings: List[VisualFinding] = []
    raw_findings = payload.get("findings", [])
    if isinstance(raw_findings, list):
        for item in raw_findings:
            if not isinstance(item, dict):
                continue
            issue = str(item.get("issue", "") or "")
            if not issue:
                continue
            findings.append(
                VisualFinding(
                    route=str(item.get("route", "") or ""),
                    issue=issue,
                    severity=_severity(item.get("severity")),
                )
            )
    return VisualReport(
        findings=findings,
        summary=str(payload.get("summary", "") or ""),
        routes=list(routes),
    )


@dataclass
class AnthropicVisualReviewer:
    """A :class:`VisualReviewer` that critiques screenshots with a vision model.

    Calls the ``anthropic`` API SDK's multimodal Messages API (base64 image
    blocks). The client is injectable so the orchestration — build content,
    parse the reply, meter the spend — is fully tested without a network call;
    only the lazy construction of the real ``AsyncAnthropic`` client is CI-only.

    When a :class:`~dev_team.budget.Budget` is supplied the call is metered
    through it (check-before / record-after, the same contract the rest of the
    engine uses) so this out-of-band vision spend is honestly accounted for —
    a pre-flight :meth:`~dev_team.budget.Budget.check` refuses the call when the
    budget is already exhausted, and the actual token cost is recorded after.
    """

    model: str = "claude-opus-4-8"
    max_tokens: int = 2048
    budget: Optional["Budget"] = None
    client: Optional[Any] = None

    def _make_client(self) -> Any:
        if self.client is not None:
            return self.client
        from anthropic import AsyncAnthropic  # pragma: no cover - real SDK, CI-only

        return AsyncAnthropic()  # pragma: no cover - real SDK, CI-only

    async def critique(
        self, screenshots: Sequence[Screenshot], rubric: str
    ) -> VisualReport:
        if self.budget is not None:
            self.budget.check()
        client = self._make_client()
        response = await client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": _build_content(screenshots, rubric)}],
        )
        report = _report_from_payload(
            _payload_from_response(response), [s.route for s in screenshots]
        )
        if self.budget is not None:
            usage = response.usage
            self.budget.record(
                "visual",
                AgentResult(
                    text=report.summary,
                    cost_usd=_usage_cost(usage.input_tokens, usage.output_tokens, self.model),
                    num_turns=1,
                    model=self.model,
                ),
            )
        return report
