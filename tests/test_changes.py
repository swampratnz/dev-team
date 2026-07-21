"""Tests for applying implementation changes to a workspace."""

from __future__ import annotations

from dev_team.changes import ChangeApplier, is_ci_workflow_path
from dev_team.execution import InMemoryWorkspace
from dev_team.models import ChangeType, FileChange, Implementation


def _impl(files):
    return Implementation(task_id="T1", summary="s", files=files)


def test_is_ci_workflow_path_matches_the_workflows_directory():
    assert is_ci_workflow_path(".github/workflows/ci.yml") is True
    assert is_ci_workflow_path("./.github/workflows/ci.yml") is True


def test_is_ci_workflow_path_matches_differently_spelled_equivalent_paths():
    # These all collapse to the same real write target as the canonical
    # form under execution._normalise (double slash, a "./" segment in the
    # middle, and Windows-style backslash separators), so the filter must
    # recognise them too or an unauthorized workflow file slips through.
    assert is_ci_workflow_path(".github//workflows/ci.yml") is True
    assert is_ci_workflow_path(".github/./workflows/ci.yml") is True
    assert is_ci_workflow_path(".github\\workflows\\ci.yml") is True


def test_is_ci_workflow_path_rejects_other_paths():
    assert is_ci_workflow_path("Dockerfile") is False
    assert is_ci_workflow_path(".github/ISSUE_TEMPLATE/bug.md") is False


def test_apply_create_and_modify():
    ws = InMemoryWorkspace()
    impl = _impl(
        [
            FileChange("a.py", ChangeType.CREATE, "adds", "x = 1"),
            FileChange("b.py", ChangeType.MODIFY, "edits", "y = 2"),
        ]
    )
    result = ChangeApplier(ws).apply(impl)
    assert result.all_applied
    assert set(result.applied_paths) == {"a.py", "b.py"}
    assert ws.read_text("a.py") == "x = 1"
    assert ws.read_text("b.py") == "y = 2"


def test_apply_delete_present_and_absent():
    ws = InMemoryWorkspace({"gone.py": "old"})
    impl = _impl(
        [
            FileChange("gone.py", ChangeType.DELETE, "remove"),
            FileChange("never.py", ChangeType.DELETE, "remove"),
        ]
    )
    result = ChangeApplier(ws).apply(impl)
    assert result.all_applied
    assert not ws.exists("gone.py")
    details = {c.path: c.detail for c in result.changes}
    assert details["gone.py"] == "deleted"
    assert details["never.py"] == "already absent"


def test_apply_empty_path_is_skipped():
    ws = InMemoryWorkspace()
    result = ChangeApplier(ws).apply(_impl([FileChange("", ChangeType.CREATE, "x", "c")]))
    assert result.all_applied is False
    assert result.applied_paths == []
    assert result.changes[0].detail == "empty path"
