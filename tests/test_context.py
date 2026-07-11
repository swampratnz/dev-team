"""Tests for the repo context builder."""

from __future__ import annotations

from dev_team.context import build_repo_context
from dev_team.execution import InMemoryWorkspace


def test_empty_workspace_renders_nothing():
    ctx = build_repo_context(InMemoryWorkspace())
    assert ctx.is_empty
    assert ctx.render() == ""


def test_context_includes_tree_manifests_and_tests():
    ws = InMemoryWorkspace(
        {
            "README.md": "# My Service\nDoes things.",
            "pyproject.toml": "[project]\nname='svc'",
            "src/app.py": "x = 1",
            "tests/test_app.py": "def test(): pass",
            ".dev_team/memory.json": "{}",
        }
    )
    ctx = build_repo_context(ws)
    assert ctx.total_files == 4  # internal bookkeeping excluded
    rendered = ctx.render()
    assert "src/app.py" in rendered
    assert "# My Service" in rendered
    assert "Tests live under: tests" in rendered
    assert ".dev_team" not in rendered


def test_context_caps_tree_and_truncates_manifests():
    files = {f"src/m{i}.py": "x" for i in range(300)}
    files["README.md"] = "A" * 5000
    ctx = build_repo_context(InMemoryWorkspace(files), max_tree_entries=10, manifest_head_chars=100)
    assert len(ctx.files) == 10
    assert ctx.total_files == 301
    rendered = ctx.render()
    assert "and 291 more" in rendered
    assert "(truncated)" in rendered


def test_context_detects_test_files_outside_tests_dir():
    ctx = build_repo_context(InMemoryWorkspace({"src/test_x.py": "x"}))
    assert ctx.test_paths == ["src"]
