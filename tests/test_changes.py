"""Tests for applying implementation changes to a workspace."""

from __future__ import annotations

from dev_team.changes import ChangeApplier
from dev_team.execution import InMemoryWorkspace
from dev_team.models import ChangeType, FileChange, Implementation


def _impl(files):
    return Implementation(task_id="T1", summary="s", files=files)


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
