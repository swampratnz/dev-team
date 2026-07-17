"""Structural, cross-reference, and secret-hygiene checks for docs/SECURITY.md."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SECURITY_DOC = _REPO_ROOT / "docs" / "SECURITY.md"

# AC1: the seven required section headings, in order.
_REQUIRED_HEADINGS = [
    "Untrusted-content & prompt-injection handling",
    "Credential & token hygiene",
    "Execution containment",
    "Workspace & path containment",
    "HTTP surface auth",
    "Pipeline/CI guardrails",
    "What this does NOT protect against",
]

# AC5: no secret-shaped content.
_BEARER_SECRET_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]{10,}")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/_-]{32,}\b")

# A backtick-fenced dotted reference of the shape ``dev_team.x.y``.
_DOTTED_REF_RE = re.compile(r"`(dev_team(?:\.\w+)+)`")


def _security_text() -> str:
    return _SECURITY_DOC.read_text(encoding="utf-8")


def _resolve(dotted_path: str):
    """Resolve ``dotted_path`` against the installed ``dev_team`` package.

    Tries the longest importable module prefix first, then walks the
    remaining dotted components with ``getattr`` — this is what lets a
    reference like ``dev_team.execution.LocalWorkspace.delete`` (a class
    attribute, not a submodule) resolve correctly. Raises ``ImportError`` or
    ``AttributeError`` if any part of the path does not exist.
    """

    parts = dotted_path.split(".")
    last_error: Exception = ImportError(f"no importable prefix in {dotted_path!r}")
    for split in range(len(parts), 0, -1):
        module_name = ".".join(parts[:split])
        try:
            obj = importlib.import_module(module_name)
        except ImportError as exc:
            last_error = exc
            continue
        for attr in parts[split:]:
            obj = getattr(obj, attr)
        return obj
    raise last_error


def test_security_doc_exists():
    assert _SECURITY_DOC.is_file(), _SECURITY_DOC


def test_security_doc_has_required_sections_in_order():
    text = _security_text()
    positions = []
    for heading in _REQUIRED_HEADINGS:
        marker = f"## {heading}"
        pos = text.find(marker)
        assert pos != -1, heading
        positions.append(pos)
    assert positions == sorted(positions)


def test_security_doc_cites_resolvable_dev_team_references():
    text = _security_text()
    refs = _DOTTED_REF_RE.findall(text)
    assert refs, "expected at least one backtick-fenced dev_team.x.y reference"
    for ref in refs:
        _resolve(ref)  # raises if the reference doesn't exist


def test_security_doc_cites_at_least_one_reference_per_module_area():
    text = _security_text()
    refs = set(_DOTTED_REF_RE.findall(text))
    expected_modules = {
        "dev_team.fences",
        "dev_team.sources",
        "dev_team.sandbox",
        "dev_team.execution",
        "dev_team.dispatch",
        "dev_team.dashboard",
    }
    cited_modules = {".".join(ref.split(".")[:2]) for ref in refs}
    assert expected_modules <= cited_modules


@pytest.mark.parametrize(
    "bogus_ref",
    ["dev_team.fences.not_a_real_symbol", "dev_team.not_a_real_module.thing"],
)
def test_resolver_reports_failure_for_a_bogus_reference(bogus_ref):
    with pytest.raises((ImportError, AttributeError)):
        _resolve(bogus_ref)


def test_security_doc_has_no_secret_shaped_content():
    text = _security_text()
    assert _BEARER_SECRET_RE.search(text) is None
    for match in _LONG_TOKEN_RE.finditer(text):
        pytest.fail(f"token-shaped literal found: {match.group(0)!r}")


def test_security_doc_names_the_documented_gaps():
    text = _security_text()
    gap_section = text.split("## What this does NOT protect against", 1)[1].lower()
    assert "per-job isolation" in gap_section
    assert "unauthenticated" in gap_section
    assert "localhost" in gap_section


def test_readme_links_security_doc():
    readme_text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"\[docs/SECURITY\.md\]\(docs/SECURITY\.md\)", readme_text)


def test_changelog_mentions_security_doc():
    changelog_text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "docs/SECURITY.md" in changelog_text
