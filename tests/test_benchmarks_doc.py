"""Drift checks for docs/BENCHMARKS.md against docs/ROADMAP.md and reality.

Mirrors tests/test_docs.py's dataclass-fields / build_parser()._actions
introspection pattern used for the docker-gate claims: a doc bullet claiming
a technique shipped must be grounded in a real config field and CLI flag,
not just plausible-sounding prose.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARKS = _REPO_ROOT / "docs" / "BENCHMARKS.md"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"

_SECTION_RE = re.compile(r"^## .*$", re.MULTILINE)


def _benchmarks_text() -> str:
    return _BENCHMARKS.read_text(encoding="utf-8")


def _changelog_text() -> str:
    return _CHANGELOG.read_text(encoding="utf-8")


def _section_text(heading: str) -> str:
    """Slice the text between a `## <heading>` line and the next `## ` line."""
    text = _benchmarks_text()
    start_match = re.search(rf"^## {re.escape(heading)}$", text, re.MULTILINE)
    assert start_match, f"heading {heading!r} not found in {_BENCHMARKS}"
    start = start_match.end()
    next_match = _SECTION_RE.search(text, start)
    end = next_match.start() if next_match else len(text)
    return text[start:end]


def _unreleased_section_text() -> str:
    text = _changelog_text()
    start = text.index("## [Unreleased]")
    next_release = re.search(r"^## \[", text[start + len("## [Unreleased]") :], re.MULTILINE)
    end = start + len("## [Unreleased]") + next_release.start() if next_release else len(text)
    return text[start:end]


def test_stale_roadmap_markers_are_gone():
    text = _benchmarks_text()
    assert "session continuity across attempts → roadmap" not in text
    assert "Dynamic re-planning on failure → roadmap" not in text
    assert (
        "multi-candidate generation with execution-based\n  reranking → roadmap"
        not in text
    )


def test_engineer_section_documents_session_continuity_as_shipped():
    text = _section_text("Engineer")
    assert "session continuity across attempts ✅" in text
    assert "EngineConfig.reuse_engineer_session" in text
    assert "ROADMAP #5" in text


def test_pm_section_documents_dynamic_replanning_as_shipped():
    text = _section_text("Product manager / planner")
    assert "Dynamic re-planning on failure ✅" in text
    assert "EngineConfig.max_replan_rounds" in text
    assert "ROADMAP #3" in text


def test_engineer_section_documents_candidate_rescue_as_shipped():
    # #291: the one previously-unclaimed BENCHMARKS.md Engineer-row line.
    text = _section_text("Engineer")
    assert "multi-candidate generation with execution-based" in text
    assert "reranking ✅" in text
    assert "EngineConfig.candidate_rescue_count" in text
    assert "--candidate-rescue" in text


def test_cited_config_fields_are_real_on_engineconfig():
    from dev_team.engine import EngineConfig

    field_names = {f.name for f in dataclasses.fields(EngineConfig)}
    assert "reuse_engineer_session" in field_names
    assert "max_replan_rounds" in field_names
    assert "candidate_rescue_count" in field_names


def test_cited_cli_flags_are_real_on_build_parser():
    from dev_team.cli import build_parser

    known_flags = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    assert "--no-reuse-engineer-session" in known_flags
    assert "--max-replan-rounds" in known_flags
    assert "--candidate-rescue" in known_flags


def test_still_deferred_techniques_were_not_overcorrected():
    text = _benchmarks_text()
    assert "Proof-of-vulnerability → roadmap" in text


def test_changelog_unreleased_mentions_the_benchmarks_correction():
    text = _unreleased_section_text()
    assert "docs/BENCHMARKS.md" in text
    assert "ROADMAP.md" in text
