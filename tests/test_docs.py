"""Structural and secret-hygiene checks for docs/TROUBLESHOOTING.md."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TROUBLESHOOTING = _REPO_ROOT / "docs" / "TROUBLESHOOTING.md"

# AC2: cross-referenced repo paths the runbook names must actually exist.
_CROSS_REFERENCED_PATHS = [
    "DEPLOYMENT.md",
    "docs/DISPATCH.md",
    "docs/DASHBOARD.md",
    "docs/PIPELINE.md",
    "docs/INTERACTION.md",
]

# AC3: one required heading substring per symptom section from AC1.
_REQUIRED_HEADINGS = [
    "401 Invalid bearer token",
    "the queue looks wrong after a restart",
    "access/request log",
    "labelled `needs-human`",
    "stuck `blocked` and never resumes",
    "HTTP status quick-reference",
    "My --interactive-pr-comments reply never got answered",
]

# AC4: closed set of secret-shaped literal substrings.
_SECRET_LITERALS = ["ghp_", "gho_", "github_pat_", "sk-ant-"]

# AC4: a live Authorization: Bearer value that isn't one of the four
# allowed placeholders.
_BEARER_SECRET_RE = re.compile(
    r"Authorization:\s*Bearer\s+"
    r"(?!<token>\b|<TOKEN>\b|\$TOKEN\b|YOUR_TOKEN\b)[A-Za-z0-9_\-\.]{16,}"
)

_DISPATCH = _REPO_ROOT / "docs" / "DISPATCH.md"
_DASHBOARD = _REPO_ROOT / "docs" / "DASHBOARD.md"

# Drift-check: every backtick-fenced `GET|POST /...` route TROUBLESHOOTING.md
# cites, to verify against the union of docs/DISPATCH.md's and
# docs/DASHBOARD.md's text (TROUBLESHOOTING.md cross-links both).
_ROUTE_RE = re.compile(r"`((?:GET|POST) /[\w{}/-]+)`")


def _troubleshooting_text() -> str:
    return _TROUBLESHOOTING.read_text(encoding="utf-8")


def _dispatch_text() -> str:
    return _DISPATCH.read_text(encoding="utf-8")


def _dashboard_text() -> str:
    return _DASHBOARD.read_text(encoding="utf-8")


def _routes_cited_in(text: str) -> set[str]:
    return set(_ROUTE_RE.findall(text))


def test_troubleshooting_doc_exists():
    assert _TROUBLESHOOTING.is_file(), _TROUBLESHOOTING


def test_troubleshooting_cross_links_resolve():
    for rel_path in _CROSS_REFERENCED_PATHS:
        assert (_REPO_ROOT / rel_path).is_file(), rel_path


def test_troubleshooting_has_all_required_sections():
    text = _troubleshooting_text()
    for heading in _REQUIRED_HEADINGS:
        assert heading in text, heading


def test_troubleshooting_has_no_secret_shaped_content():
    text = _troubleshooting_text()
    for literal in _SECRET_LITERALS:
        assert literal not in text, literal
    assert _BEARER_SECRET_RE.search(text) is None


def test_deployment_gotcha_callouts_both_cross_reference_troubleshooting():
    deployment_text = (_REPO_ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert deployment_text.count("(docs/TROUBLESHOOTING.md)") == 2


def test_changelog_mentions_troubleshooting_runbook():
    changelog_text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "docs/TROUBLESHOOTING.md" in changelog_text


def test_access_log_section_documents_the_shipped_route():
    text = _troubleshooting_text()
    assert "GET /access-log" in text
    assert "no HTTP route" not in text


def test_job_vanished_section_documents_the_shipped_cancel_route():
    text = _troubleshooting_text()
    assert "POST /jobs/{id}/cancel" in text
    assert 'cancel a queued job" workaround' not in text


def test_routes_cited_in_ignores_unfenced_prose():
    text = "We use GET and POST verbs for HTTP requests, but not shown here."
    assert _routes_cited_in(text) == set()


def test_routes_cited_in_deduplicates():
    text = (
        "See `GET /access-log` for the log, and again `GET /access-log`. "
        "Also `POST /jobs/{id}/cancel` to cancel a queued job."
    )
    assert _routes_cited_in(text) == {"GET /access-log", "POST /jobs/{id}/cancel"}


def test_troubleshooting_routes_are_documented_in_dispatch():
    routes = _routes_cited_in(_troubleshooting_text())
    dispatch_text = _dispatch_text()
    dashboard_text = _dashboard_text()
    assert {
        "GET /access-log",
        "POST /jobs/{id}/cancel",
        "POST /backlog/story/{id}/status",
    } <= routes
    for route in routes:
        assert route in dispatch_text or route in dashboard_text, route


def test_a_bogus_route_is_not_documented_in_dispatch():
    assert "GET /not-a-real-route" not in _dispatch_text()
    assert "GET /not-a-real-route" not in _dashboard_text()


def test_blocked_story_section_documents_the_recovery_route():
    text = _troubleshooting_text()
    assert "POST /backlog/story/{id}/status" in text


def test_status_table_documents_foreman_500():
    text = _troubleshooting_text()
    lines_with_500_and_foreman_run = [
        line
        for line in text.splitlines()
        if "500" in line and "POST /foreman/run" in line
    ]
    assert lines_with_500_and_foreman_run


def test_pr_comment_section_documents_the_allowlist_and_failsafe():
    text = _troubleshooting_text()
    assert "--interactive-pr-comment-author" in text
    assert "`apply`" in text
    assert "`skip`" in text
    assert "fail" in text


def test_pr_comment_section_cross_links_interaction_doc():
    text = _troubleshooting_text()
    assert "(docs/INTERACTION.md)" in text
