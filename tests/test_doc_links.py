"""Tests for ``broken_relative_links`` — cross-doc Markdown link integrity.

Replaces the maintenance burden of ``test_docs.py``'s hand-listed
``_CROSS_REFERENCED_PATHS`` with a check that parses every doc's actual
``[text](path)`` link syntax and resolves it relative to the citing doc's
own directory, so a dead cross-link from a rename/move is caught
automatically instead of only for the 5 paths someone remembered to list.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from dev_team.agents.techwriter import broken_relative_links

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_findings_when_all_links_resolve():
    doc_contents = {"docs/A.md": "See [B](B.md) for details."}
    assert broken_relative_links(doc_contents, ["docs/B.md"]) == []


def test_one_finding_per_broken_link_names_doc_and_target():
    doc_contents = {"docs/A.md": "See [Missing](MISSING.md) for details."}
    issues = broken_relative_links(doc_contents, [])
    assert len(issues) == 1
    assert "docs/A.md" in issues[0]
    assert "MISSING.md" in issues[0]


def test_multiple_broken_links_each_get_their_own_finding():
    doc_contents = {"docs/A.md": "[One](ONE.md) and [Two](TWO.md)."}
    issues = broken_relative_links(doc_contents, [])
    assert len(issues) == 2


def test_http_and_https_link_targets_never_flagged():
    doc_contents = {
        "docs/A.md": "[a](https://example.com/x) and [b](http://example.com/y)."
    }
    assert broken_relative_links(doc_contents, []) == []


def test_mailto_link_targets_never_flagged():
    doc_contents = {"docs/A.md": "[email](mailto:someone@example.com)."}
    assert broken_relative_links(doc_contents, []) == []


def test_pure_same_page_anchor_never_flagged():
    doc_contents = {"docs/A.md": "[section](#usage)."}
    assert broken_relative_links(doc_contents, []) == []


def test_trailing_anchor_on_a_real_file_is_not_flagged():
    doc_contents = {"docs/A.md": "[auth](B.md#auth)."}
    assert broken_relative_links(doc_contents, ["docs/B.md"]) == []


def test_trailing_anchor_on_a_missing_file_is_still_flagged():
    doc_contents = {"docs/A.md": "[auth](B.md#auth)."}
    issues = broken_relative_links(doc_contents, [])
    assert len(issues) == 1
    assert "B.md" in issues[0]


def test_relative_resolution_is_against_the_citing_docs_own_directory():
    # The same target "B.md", cited from a doc under docs/ vs. a repo-root
    # doc, must resolve to two different candidate paths: only "B.md" (repo
    # root) is known, so the docs/-relative citation is broken while the
    # root-relative one resolves.
    from_docs_subdir = broken_relative_links({"docs/A.md": "[b](B.md)"}, ["B.md"])
    from_repo_root = broken_relative_links({"A.md": "[b](B.md)"}, ["B.md"])
    assert len(from_docs_subdir) == 1
    assert from_repo_root == []


def test_malformed_or_empty_link_syntax_is_skipped_without_raising():
    doc_contents = {"docs/A.md": "[empty]() and an unterminated [bracket"}
    assert broken_relative_links(doc_contents, []) == []


def test_no_filesystem_io_results_are_driven_purely_by_set_membership():
    # These candidate paths do not exist on disk in this test run — if
    # broken_relative_links touched the real filesystem instead of the
    # known_files argument, the "known" case below would incorrectly flag.
    doc_contents = {"docs/A.md": "[x](nonexistent-dir/ghost.md)"}
    assert broken_relative_links(doc_contents, ["docs/nonexistent-dir/ghost.md"]) == []
    issues = broken_relative_links(doc_contents, [])
    assert len(issues) == 1
    assert "ghost.md" in issues[0]


def _tracked_files():
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_whole_docs_corpus_has_zero_broken_relative_links():
    tracked = _tracked_files()
    doc_paths = [
        path
        for path in tracked
        if path in ("README.md", "CLAUDE.md")
        or (path.startswith("docs/") and path.endswith(".md") and "/" not in path[len("docs/") :])
    ]
    assert len(doc_paths) >= 10  # sanity: the glob actually found the real docs
    doc_contents = {
        path: (_REPO_ROOT / path).read_text(encoding="utf-8") for path in doc_paths
    }
    assert broken_relative_links(doc_contents, tracked) == []
