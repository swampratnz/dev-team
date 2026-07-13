"""Tests for the repo context builder."""

from __future__ import annotations

from dev_team.context import build_repo_context
from dev_team.execution import InMemoryWorkspace, LocalWorkspace


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


def test_context_reports_actual_containing_directory():
    ctx = build_repo_context(InMemoryWorkspace({"src/pkg/test_utils.py": "x"}))
    assert ctx.test_paths == ["src/pkg"]


def test_context_reports_root_for_top_level_test_file():
    ctx = build_repo_context(InMemoryWorkspace({"test_x.py": "x"}))
    assert ctx.test_paths == ["."]


def test_context_fences_manifest_heads():
    ctx = build_repo_context(InMemoryWorkspace({"README.md": "# Svc"}))
    rendered = ctx.render()
    assert '<manifest-content name="README.md">' in rendered
    assert "</manifest-content>" in rendered


def test_context_skips_unreadable_manifest(tmp_path):
    # A non-UTF-8 (or otherwise unreadable) root manifest must not unwind the
    # whole read-only assess() run: the bad file is skipped and the remaining
    # manifests and tree still build.
    (tmp_path / "README.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='svc'")
    (tmp_path / "app.py").write_text("x = 1")
    ctx = build_repo_context(LocalWorkspace(str(tmp_path)))
    # The unreadable manifest is dropped from the heads, without raising...
    assert "README.md" not in ctx.manifest_heads
    # ...while readable manifests are still captured.
    assert "pyproject.toml" in ctx.manifest_heads
    # ...and the file still appears in the tree listing.
    assert "README.md" in ctx.files
