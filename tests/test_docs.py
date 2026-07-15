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
]

# AC3: one required heading substring per symptom section from AC1.
_REQUIRED_HEADINGS = [
    "401 Invalid bearer token",
    "the queue looks wrong after a restart",
    "access/request log",
    "labelled `needs-human`",
    "HTTP status quick-reference",
]

# AC4: closed set of secret-shaped literal substrings.
_SECRET_LITERALS = ["ghp_", "gho_", "github_pat_", "sk-ant-"]

# AC4: a live Authorization: Bearer value that isn't one of the four
# allowed placeholders.
_BEARER_SECRET_RE = re.compile(
    r"Authorization:\s*Bearer\s+"
    r"(?!<token>\b|<TOKEN>\b|\$TOKEN\b|YOUR_TOKEN\b)[A-Za-z0-9_\-\.]{16,}"
)


def _troubleshooting_text() -> str:
    return _TROUBLESHOOTING.read_text(encoding="utf-8")


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
