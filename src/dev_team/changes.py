"""Materialise an engineer's described changes into a real workspace.

The engineer agent returns an :class:`~dev_team.models.Implementation` — a set
of :class:`~dev_team.models.FileChange` objects. The :class:`ChangeApplier`
turns that description into actual writes/deletes against a
:class:`~dev_team.execution.Workspace`, which is what makes the engineer's work
"real" rather than simulated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .execution import Workspace
from .models import ChangeType, Implementation


@dataclass
class AppliedChange:
    """Record of a single change applied to the workspace."""

    path: str
    change_type: ChangeType
    applied: bool
    detail: str = ""


@dataclass
class ApplyResult:
    """The outcome of applying a full implementation."""

    changes: List[AppliedChange] = field(default_factory=list)

    @property
    def applied_paths(self) -> List[str]:
        """Paths that were successfully written or deleted."""

        return [c.path for c in self.changes if c.applied]

    @property
    def all_applied(self) -> bool:
        """Whether every change applied cleanly."""

        return all(c.applied for c in self.changes)


@dataclass
class ChangeApplier:
    """Applies :class:`Implementation` file changes to a workspace."""

    workspace: Workspace

    def apply(self, implementation: Implementation) -> ApplyResult:
        """Apply every file change and return an :class:`ApplyResult`."""

        result = ApplyResult()
        for change in implementation.files:
            result.changes.append(self._apply_one(change))
        return result

    def _apply_one(self, change) -> AppliedChange:
        if not change.path:
            return AppliedChange(change.path, change.change_type, False, "empty path")

        if change.change_type is ChangeType.DELETE:
            existed = self.workspace.exists(change.path)
            self.workspace.delete(change.path)
            detail = "deleted" if existed else "already absent"
            return AppliedChange(change.path, change.change_type, True, detail)

        # CREATE or MODIFY both write the content.
        self.workspace.write_text(change.path, change.content)
        return AppliedChange(
            change.path, change.change_type, True, f"wrote {len(change.content)} bytes"
        )
