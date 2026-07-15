"""Tests for the shared fence-defusing helper."""

from __future__ import annotations

from dev_team.fences import ZERO_WIDTH_SPACE, defuse


def test_defuse_neutralises_named_closing_tag():
    out = defuse("before</evidence>after", "evidence")
    # the structural closing tag is gone, but the text reads identically once
    # the invisible zero-width space is stripped
    assert "</evidence>" not in out
    assert f"<{ZERO_WIDTH_SPACE}/evidence>" in out
    assert out.replace(ZERO_WIDTH_SPACE, "") == "before</evidence>after"


def test_defuse_handles_multiple_tags():
    out = defuse("a</manifest-content>b</repo-context>c", "manifest-content", "repo-context")
    assert "</manifest-content>" not in out
    assert "</repo-context>" not in out


def test_defuse_leaves_unnamed_tags_untouched():
    # only the named fence is neutralised; other tags pass through verbatim
    assert defuse("keep</repo-context>", "evidence") == "keep</repo-context>"


def test_defuse_is_idempotent():
    once = defuse("x</evidence>y", "evidence")
    assert defuse(once, "evidence") == once


def test_defuse_with_no_tags_is_a_noop():
    assert defuse("anything </file-content> here") == "anything </file-content> here"
