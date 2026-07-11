"""Shared team memory: a blackboard, decision log, and cross-run persistence.

Real teams share context. The :class:`Blackboard` is working memory every agent
can read and write; :class:`DecisionRecord` captures architecture decisions
(ADRs); :class:`ProjectMemory` persists a durable summary to the workspace so a
later run can pick up where the last one left off.
"""

from __future__ import annotations

import hashlib
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
        self._decision_seq = 0

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
    def seed_decision_ids(self, count: int) -> None:
        """Continue ADR numbering after ``count`` decisions from earlier runs.

        Without this every run restarts at ``ADR-001`` and the persisted
        decision log fills with colliding ids.
        """

        self._decision_seq = max(self._decision_seq, count)

    def record_decision(
        self,
        title: str,
        context: str,
        decision: str,
        consequences: str = "",
    ) -> DecisionRecord:
        """Append a decision record with an auto-assigned id."""

        self._decision_seq += 1
        record = DecisionRecord(
            id=f"ADR-{self._decision_seq:03d}",
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


_CHECKPOINT_DIR = ".dev_team"


def task_fingerprint(title: str, description: str) -> str:
    """A stable identity for a task's *content* (not just its id).

    Plans are regenerated on resume, and nothing guarantees the new plan's
    ``T1`` describes the same work as the old plan's ``T1``. A task is only
    treated as already-done when both the id and this fingerprint match.
    """

    digest = hashlib.sha256(f"{title}\n{description}".encode("utf-8"))
    return digest.hexdigest()[:16]


@dataclass
class RunCheckpoint:
    """Durable progress of an in-flight delivery run.

    Beyond task completion it carries the run's *plan* (so a resume works the
    same tasks instead of gambling on a regenerated plan reproducing them
    byte-for-byte) and the git ``baseline_sha`` the delivery started from (so
    the final squashed feature commit spans the original run's work too).
    """

    feature_title: str
    done_task_ids: List[str] = field(default_factory=list)
    fingerprints: Dict[str, str] = field(default_factory=dict)
    baseline_sha: Optional[str] = None
    plan: Optional[Dict[str, Any]] = None

    def mark_done(self, task_id: str, fingerprint: str) -> None:
        """Record a completed task with its content fingerprint."""

        self.done_task_ids.append(task_id)
        self.fingerprints[task_id] = fingerprint

    def is_done(self, task_id: str, fingerprint: str) -> bool:
        """Whether ``task_id`` completed earlier *with the same content*."""

        return (
            task_id in self.done_task_ids
            and self.fingerprints.get(task_id) == fingerprint
        )


@dataclass
class CheckpointStore:
    """Persists a :class:`RunCheckpoint` so a crashed run can resume.

    The delivery engine records each task as it completes; a later run for the
    same feature skips tasks already done instead of re-paying for them.
    Checkpoints are stored one file per feature, so starting a different
    feature never clobbers an interrupted run's progress.
    """

    workspace: Workspace
    directory: str = _CHECKPOINT_DIR

    def _path_for(self, feature_title: str) -> str:
        return f"{self.directory}/checkpoint-{task_fingerprint(feature_title, '')}.json"

    def save(self, checkpoint: RunCheckpoint) -> None:
        """Write ``checkpoint`` to the workspace as JSON."""

        payload = {
            "feature_title": checkpoint.feature_title,
            "done_task_ids": list(checkpoint.done_task_ids),
            "fingerprints": dict(checkpoint.fingerprints),
            "baseline_sha": checkpoint.baseline_sha,
            "plan": checkpoint.plan,
        }
        self.workspace.write_text(
            self._path_for(checkpoint.feature_title), json.dumps(payload, indent=2)
        )

    def load(self, feature_title: str) -> RunCheckpoint:
        """Load the checkpoint for ``feature_title``, or an empty one.

        A stored checkpoint for a *different* feature is ignored — resuming
        someone else's progress would silently skip real work. Checkpoints
        without fingerprints (older format) never match, and a corrupt file
        (e.g. truncated by a crash predating atomic writes) reads as no
        checkpoint — both are the safe direction: work is redone rather than
        wrongly skipped, and a resume never dies on a traceback.
        """

        path = self._path_for(feature_title)
        if not self.workspace.exists(path):
            return RunCheckpoint(feature_title=feature_title)
        try:
            data = json.loads(self.workspace.read_text(path))
        except ValueError:
            return RunCheckpoint(feature_title=feature_title)
        if not isinstance(data, dict) or data.get("feature_title") != feature_title:
            return RunCheckpoint(feature_title=feature_title)
        plan = data.get("plan")
        return RunCheckpoint(
            feature_title=feature_title,
            done_task_ids=[str(t) for t in data.get("done_task_ids", [])],
            fingerprints={
                str(k): str(v) for k, v in data.get("fingerprints", {}).items()
            },
            baseline_sha=(
                str(data["baseline_sha"]) if data.get("baseline_sha") else None
            ),
            plan=plan if isinstance(plan, dict) else None,
        )

    def clear(self, feature_title: str) -> None:
        """Remove the stored checkpoint for ``feature_title``."""

        self.workspace.delete(self._path_for(feature_title))


_MEMORY_PATH = ".dev_team/memory.json"

# Bounded history: memory must not grow without limit across runs.
_MAX_DECISIONS = 50
_MAX_RETRO_NOTES = 20


@dataclass
class ProjectMemory:
    """Persists a durable summary of runs to the workspace.

    ``save`` *merges* the run into what is already stored — decisions (ADRs)
    and retrospective notes accumulate across runs (bounded) instead of each
    run erasing the one before, which is what makes the memory genuinely
    cross-run rather than a one-run horizon.
    """

    workspace: Workspace
    path: str = _MEMORY_PATH

    def save(self, blackboard: Blackboard) -> None:
        """Merge the blackboard snapshot into the stored memory."""

        prior = self.load() or {}
        snapshot = blackboard.snapshot()

        seen: set = set()
        decisions: List[Dict[str, Any]] = []
        for record in list(prior.get("decisions") or []) + snapshot["decisions"]:
            if record.get("id") in seen:
                continue
            seen.add(record.get("id"))
            decisions.append(record)

        prior_entries = prior.get("entries") or {}
        retro = list(prior_entries.get("retrospective") or []) + list(
            snapshot["entries"].get("retrospective") or []
        )
        if retro:
            snapshot["entries"]["retrospective"] = retro[-_MAX_RETRO_NOTES:]

        payload = {
            "entries": snapshot["entries"],
            "artifacts": snapshot["artifacts"],
            "decisions": decisions[-_MAX_DECISIONS:],
            "runs": int(prior.get("runs", 0)) + 1,
        }
        self.workspace.write_text(self.path, json.dumps(payload, indent=2))

    def load(self) -> Optional[Dict[str, Any]]:
        """Load the stored memory, or ``None`` if absent or unreadable."""

        if not self.workspace.exists(self.path):
            return None
        try:
            data = json.loads(self.workspace.read_text(self.path))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
