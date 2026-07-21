"""Materialise an engineer's described changes into a real workspace.

The engineer agent returns an :class:`~dev_team.models.Implementation` — a set
of :class:`~dev_team.models.FileChange` objects. The :class:`ChangeApplier`
turns that description into actual writes/deletes against a
:class:`~dev_team.execution.Workspace`, which is what makes the engineer's work
"real" rather than simulated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

from .execution import Workspace
from .models import ChangeType, Implementation

#: The segments identifying GitHub Actions' workflow directory once a path is
#: split the same way ``execution._normalise`` splits it. Shared by the
#: DevOps artifact filter (default-deny CI workflow authorship,
#: ``EngineConfig.allow_ci_workflows``) and the push step's PAT-scope
#: warning, which both need to recognise the identical path shape.
CI_WORKFLOW_SEGMENTS = (".github", "workflows")


def is_ci_workflow_path(path: str) -> bool:
    """Whether ``path`` names a GitHub Actions workflow file.

    Splits on both ``/`` and ``\\`` and drops empty/``.`` segments — the same
    normalisation ``execution._normalise`` applies before a write actually
    lands — so this check agrees with the real write target on path variants
    like ``.github//workflows/x``, ``.github/./workflows/x``, or
    Windows-style ``.github\\workflows\\x``, none of which a literal
    string-prefix check would catch.
    """

    parts = [part for part in re.split(r"[\\/]", path) if part not in ("", ".")]
    return tuple(parts[:2]) == CI_WORKFLOW_SEGMENTS


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
