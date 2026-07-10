"""Shared team memory: a blackboard, decision log, and cross-run persistence.

Real teams share context. The :class:`Blackboard` is working memory every agent
can read and write; :class:`DecisionRecord` captures architecture decisions
(ADRs); :class:`ProjectMemory` persists a durable summary to the workspace so a
later run can pick up where the last one left off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .execution import Workspace


@dataclass
class Artifact:
    """A named piece of work posted to the blackboard by an agent."""

    kind: str
    key: str
    summary: str


@dataclass
class DecisionRecord:
    """A lightweight architecture decision record (ADR)."""

    id: str
    title: str
    context: str
    decision: str
    consequences: str = ""


class Blackboard:
    """Shared working memory plus an append-only artifact and decision log."""

    def __init__(self) -> None:
        self._entries: Dict[str, Any] = {}
        self.artifacts: List[Artifact] = []
        self.decisions: List[DecisionRecord] = []

    # -- key/value working memory --
    def put(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``."""

        self._entries[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for ``key`` or ``default``."""

        return self._entries.get(key, default)

    def has(self, key: str) -> bool:
        """Whether ``key`` is present."""

        return key in self._entries

    def keys(self) -> List[str]:
        """Return the stored keys, sorted."""

        return sorted(self._entries)

    # -- artifacts --
    def post_artifact(self, kind: str, key: str, summary: str) -> Artifact:
        """Append an artifact to the shared log and return it."""

        artifact = Artifact(kind=kind, key=key, summary=summary)
        self.artifacts.append(artifact)
        return artifact

    def artifacts_of_kind(self, kind: str) -> List[Artifact]:
        """Return all artifacts of a given ``kind``."""

        return [a for a in self.artifacts if a.kind == kind]

    # -- decisions (ADRs) --
    def record_decision(
        self,
        title: str,
        context: str,
        decision: str,
        consequences: str = "",
    ) -> DecisionRecord:
        """Append a decision record with an auto-assigned id."""

        record = DecisionRecord(
            id=f"ADR-{len(self.decisions) + 1:03d}",
            title=title,
            context=context,
            decision=decision,
            consequences=consequences,
        )
        self.decisions.append(record)
        return record

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot of the whole blackboard."""

        return {
            "entries": dict(self._entries),
            "artifacts": [vars(a) for a in self.artifacts],
            "decisions": [vars(d) for d in self.decisions],
        }


_CHECKPOINT_PATH = ".dev_team/checkpoint.json"


@dataclass
class RunCheckpoint:
    """Durable progress of an in-flight delivery run."""

    feature_title: str
    done_task_ids: List[str] = field(default_factory=list)


@dataclass
class CheckpointStore:
    """Persists a :class:`RunCheckpoint` so a crashed run can resume.

    The delivery engine records each task as it completes; a later run for the
    same feature skips tasks already done instead of re-paying for them.
    """

    workspace: Workspace
    path: str = _CHECKPOINT_PATH

    def save(self, checkpoint: RunCheckpoint) -> None:
        """Write ``checkpoint`` to the workspace as JSON."""

        payload = {
            "feature_title": checkpoint.feature_title,
            "done_task_ids": list(checkpoint.done_task_ids),
        }
        self.workspace.write_text(self.path, json.dumps(payload, indent=2))

    def load(self, feature_title: str) -> RunCheckpoint:
        """Load the checkpoint for ``feature_title``, or an empty one.

        A stored checkpoint for a *different* feature is ignored — resuming
        someone else's progress would silently skip real work.
        """

        if not self.workspace.exists(self.path):
            return RunCheckpoint(feature_title=feature_title)
        data = json.loads(self.workspace.read_text(self.path))
        if data.get("feature_title") != feature_title:
            return RunCheckpoint(feature_title=feature_title)
        return RunCheckpoint(
            feature_title=feature_title,
            done_task_ids=[str(t) for t in data.get("done_task_ids", [])],
        )

    def clear(self) -> None:
        """Remove any stored checkpoint (called after a fully successful run)."""

        self.workspace.delete(self.path)


_MEMORY_PATH = ".dev_team/memory.json"


@dataclass
class ProjectMemory:
    """Persists a durable summary of a run to the workspace."""

    workspace: Workspace
    path: str = _MEMORY_PATH

    def save(self, blackboard: Blackboard) -> None:
        """Write the blackboard snapshot to the workspace as JSON."""

        self.workspace.write_text(self.path, json.dumps(blackboard.snapshot(), indent=2))

    def load(self) -> Optional[Dict[str, Any]]:
        """Load a previously saved snapshot, or ``None`` if absent."""

        if not self.workspace.exists(self.path):
            return None
        return json.loads(self.workspace.read_text(self.path))
