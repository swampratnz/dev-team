"""Structural and secret-hygiene checks for docs/TROUBLESHOOTING.md."""

from __future__ import annotations

import inspect
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
    "docker_build_verified` / `docker_run_verified` is false",
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
_ENGINE = _REPO_ROOT / "src" / "dev_team" / "engine.py"
_BENCHMARKS = _REPO_ROOT / "docs" / "BENCHMARKS.md"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

# Regression: #240 deliberately left these BENCHMARKS.md markers "→ roadmap"
# (genuinely still-deferred techniques) — a fix to a different bullet must not
# overcorrect and flip these too. Matched against whitespace-normalized text
# since the source wraps mid-phrase. #291 shipped multi-candidate generation
# with execution-based reranking (`--candidate-rescue`), so that marker moved
# out of this still-deferred list — see test_benchmarks_doc.py for its own
# drift check against the shipped feature.
_STILL_DEFERRED_MARKERS = [
    "Proof-of-vulnerability → roadmap",
]

# Drift-check: every backtick-fenced `GET|POST /...` route TROUBLESHOOTING.md
# cites, to verify against the union of docs/DISPATCH.md's and
# docs/DASHBOARD.md's text (TROUBLESHOOTING.md cross-links both).
_ROUTE_RE = re.compile(r"`((?:GET|POST) /[\w{}/-]+)`")

# Drift-check: every backtick-fenced `docker-...` event name the docker-gate
# section cites, to verify each is still a real _event(...) literal in
# engine.py rather than a name the doc invented or let go stale.
_DOCKER_EVENT_RE = re.compile(r"`(docker-[a-z-]+)`")

_DOCKER_GATE_HEADING = "docker_build_verified` / `docker_run_verified` is false"


def _troubleshooting_text() -> str:
    return _TROUBLESHOOTING.read_text(encoding="utf-8")


def _dispatch_text() -> str:
    return _DISPATCH.read_text(encoding="utf-8")


def _dashboard_text() -> str:
    return _DASHBOARD.read_text(encoding="utf-8")


def _engine_text() -> str:
    return _ENGINE.read_text(encoding="utf-8")


def _docker_gate_section_text() -> str:
    text = _troubleshooting_text()
    start = text.index(_DOCKER_GATE_HEADING)
    end = text.index("## Dashboard/dispatch HTTP status quick-reference", start)
    return text[start:end]


def _routes_cited_in(text: str) -> set[str]:
    return set(_ROUTE_RE.findall(text))


def _benchmarks_text() -> str:
    return _BENCHMARKS.read_text(encoding="utf-8")


def _technical_writer_section_text() -> str:
    text = _benchmarks_text()
    start = text.index("## Technical writer")
    end = text.index("\n## ", start + len("## Technical writer"))
    return text[start:end]


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _changelog_unreleased_section_text() -> str:
    text = _CHANGELOG.read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    end = text.index("\n## [", start + len("## [Unreleased]"))
    return text[start:end]


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


def test_docker_gate_section_documents_the_scorecard_keys():
    text = _docker_gate_section_text()
    assert "docker_build_verified" in text
    assert "docker_run_verified" in text


def test_docker_gate_scorecard_keys_are_still_real_in_engine():
    engine_text = _engine_text()
    assert '"docker_build_verified"' in engine_text
    assert '"docker_run_verified"' in engine_text


def test_docker_gate_section_documents_the_cli_flags():
    from dev_team.cli import build_parser

    text = _docker_gate_section_text()
    assert "--docker-build-gate" in text
    assert "--docker-run-gate" in text
    known_flags = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    assert "--docker-build-gate" in known_flags
    assert "--docker-run-gate" in known_flags


def test_docker_gate_section_documents_the_hardening_flags():
    text = _docker_gate_section_text()
    assert "--network none" in text
    assert "--cap-drop ALL" in text
    assert "no-new-privileges" in text


def test_docker_gate_section_states_advisory_only_and_never_blocks():
    text = _docker_gate_section_text()
    assert "advisory only" in text
    assert "never blocks" in text


def test_docker_gate_cited_events_are_real_event_literals_in_engine():
    engine_text = _engine_text()
    events = _DOCKER_EVENT_RE.findall(_docker_gate_section_text())
    assert events
    for event in events:
        assert f'"{event}"' in engine_text, event


def test_docker_gate_section_does_not_misattribute_the_exited_early_case():
    text = _docker_gate_section_text()
    assert "not `docker-run-verified`" in text


def test_benchmarks_no_longer_claims_doc_claim_checks_are_roadmap():
    assert "Executable doc-claim checks → roadmap" not in _benchmarks_text()


def test_technical_writer_section_names_doc_claim_issues_as_shipped():
    text = _technical_writer_section_text()
    assert "✅" in text
    assert "doc_claim_issues" in text


def test_doc_claim_issues_is_real_and_grounded_in_its_shipped_signature():
    from dev_team.agents import techwriter

    assert hasattr(techwriter, "doc_claim_issues")
    assert callable(techwriter.doc_claim_issues)
    params = inspect.signature(techwriter.doc_claim_issues).parameters
    assert "doc_files" in params
    assert "known_files" in params


def test_benchmarks_deferred_markers_remain_untouched():
    normalized = _normalize_whitespace(_benchmarks_text())
    for marker in _STILL_DEFERRED_MARKERS:
        assert marker in normalized


def test_changelog_unreleased_section_mentions_benchmarks_correction():
    text = _changelog_unreleased_section_text()
    assert "docs/BENCHMARKS.md" in text
    assert "Technical writer" in text
