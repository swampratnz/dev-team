"""Drift checks for docs/ROADMAP.md item 7's interaction-surfaces claims."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROADMAP = _REPO_ROOT / "docs" / "ROADMAP.md"
_DASHBOARD = _REPO_ROOT / "docs" / "DASHBOARD.md"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

_STALE_SENTENCE = "The dashboard and Slack adapters remain future work."


def _roadmap_text() -> str:
    return _ROADMAP.read_text(encoding="utf-8")


def _item_7_span() -> str:
    text = _roadmap_text()
    start = text.index("## 7. Richer interaction surfaces")
    end = text.index("## 8.", start)
    return text[start:end]


def test_roadmap_no_longer_claims_dashboard_adapter_is_future_work():
    assert _STALE_SENTENCE not in _roadmap_text()


def test_roadmap_item_7_references_the_shipped_dashboard_panel():
    assert "Pending questions" in _item_7_span()


def test_dashboard_pending_questions_heading_still_exists():
    dashboard_text = _DASHBOARD.read_text(encoding="utf-8")
    assert "### Pending questions" in dashboard_text


def test_roadmap_still_correctly_defers_the_slack_adapter():
    span = _item_7_span()
    assert "Slack" in span
    assert "future work" in span


def test_changelog_mentions_roadmap_correction():
    changelog_text = _CHANGELOG.read_text(encoding="utf-8")
    assert "docs/ROADMAP.md" in changelog_text
