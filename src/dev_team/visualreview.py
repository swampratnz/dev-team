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

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Protocol, Sequence, runtime_checkable

from .models import Severity

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
