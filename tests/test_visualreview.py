"""Tests for the visual-review seams and their in-memory fakes."""

from __future__ import annotations

from helpers import run

from dev_team.models import Severity
from dev_team.visualreview import (
    VISUAL_RUBRIC,
    AppServer,
    FakeAppServer,
    FakePageCapturer,
    FakeVisualReviewer,
    PageCapturer,
    Screenshot,
    VisualFinding,
    VisualReport,
    VisualReviewer,
)


def test_visual_report_clean_is_true_without_major_findings():
    report = VisualReport(
        findings=[VisualFinding(route="/", issue="tiny", severity=Severity.MINOR)]
    )
    assert report.clean is True


def test_visual_report_clean_is_false_with_a_major_finding():
    report = VisualReport(
        findings=[VisualFinding(route="/", issue="broken", severity=Severity.MAJOR)]
    )
    assert report.clean is False


def test_fake_app_server_yields_base_url_and_counts_starts():
    server = FakeAppServer(base_url="http://x")
    with server.serve() as base_url:
        assert base_url == "http://x"
    assert server.starts == 1


def test_fake_page_capturer_defaults_to_one_screenshot_per_route():
    capturer = FakePageCapturer()
    shots = capturer.capture("http://x", ["/", "/steps"])
    assert [s.route for s in shots] == ["/", "/steps"]
    assert all(s.png for s in shots)
    assert capturer.calls == [("http://x", ("/", "/steps"))]


def test_fake_page_capturer_returns_provided_screenshots():
    canned = [Screenshot(route="/only", png=b"x")]
    capturer = FakePageCapturer(screenshots=canned)
    assert capturer.capture("http://x", ["/", "/steps"]) == canned


def test_fake_visual_reviewer_default_report_is_clean():
    reviewer = FakeVisualReviewer()
    shots = [Screenshot(route="/", png=b"x")]
    report = run(reviewer.critique(shots, VISUAL_RUBRIC))
    assert report.findings == []
    assert report.routes == ["/"]
    assert reviewer.seen == shots


def test_fake_visual_reviewer_returns_provided_report():
    canned = VisualReport(summary="scripted")
    reviewer = FakeVisualReviewer(report=canned)
    got = run(reviewer.critique([Screenshot(route="/", png=b"x")], VISUAL_RUBRIC))
    assert got is canned


def test_fakes_satisfy_the_protocols():
    assert isinstance(FakeAppServer(), AppServer)
    assert isinstance(FakePageCapturer(), PageCapturer)
    assert isinstance(FakeVisualReviewer(), VisualReviewer)


def test_visual_rubric_is_nonempty_guidance():
    assert "screenshot" in VISUAL_RUBRIC.lower()
