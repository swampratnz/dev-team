"""A persisted score trail: per-run delivery metrics with run-over-run deltas.

Getting *better over time* needs a score history, not just a one-run scorecard.
:class:`ScoreHistory` appends a compact :class:`RunScore` for each delivery to a
bounded JSON file in the workspace, and :meth:`ScoreHistory.render` shows each
run's headline metrics with the delta from the run before it — so a prompt or
orchestration change surfaces as a movement in cost, attempts, or the scorecard
counters rather than a vibe. Deterministic and dependency-free, like the rest of
the team's memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .execution import Workspace

_SCORE_PATH = ".dev_team/score-history.json"

# Bounded history: the trail must not grow without limit across runs.
_MAX_SCORE_RUNS = 50


@dataclass
class RunScore:
    """The headline metrics of a single delivery run."""

    feature: str
    success: bool
    tasks_total: int
    tasks_succeeded: int
    total_attempts: int
    cost_usd: float
    committed: bool
    scorecard: Dict[str, int] = field(default_factory=dict)


def _run_score_from_dict(data: Dict[str, Any]) -> RunScore:
    """Build a :class:`RunScore` from stored JSON, defensively.

    Values are read with defaults so a partial or older record never crashes a
    load; ``str``/``bool`` coercion is total, and the numeric fields are stored
    (and so read back) as the JSON types we wrote.
    """

    raw = data.get("scorecard")
    return RunScore(
        feature=str(data.get("feature", "")),
        success=bool(data.get("success", False)),
        tasks_total=data.get("tasks_total", 0),
        tasks_succeeded=data.get("tasks_succeeded", 0),
        total_attempts=data.get("total_attempts", 0),
        cost_usd=data.get("cost_usd", 0.0),
        committed=bool(data.get("committed", False)),
        scorecard=raw if isinstance(raw, dict) else {},
    )


def _signed(n: int) -> str:
    """Render an integer delta with an explicit sign (``+3`` / ``-2``)."""

    return f"+{n}" if n > 0 else str(n)


def _signed_cost(delta: float) -> str:
    """Render a cost delta with an explicit sign and dollar sign."""

    return f"+${delta:.4f}" if delta > 0 else f"-${abs(delta):.4f}"


def _score_deltas(prev: RunScore, cur: RunScore) -> List[str]:
    """Human-readable deltas for the metrics that changed between two runs."""

    diffs: List[str] = []
    if cur.total_attempts != prev.total_attempts:
        diffs.append(f"attempts {_signed(cur.total_attempts - prev.total_attempts)}")
    cost_delta = cur.cost_usd - prev.cost_usd
    if abs(cost_delta) >= 1e-9:
        diffs.append(f"cost {_signed_cost(cost_delta)}")
    for key in sorted(set(prev.scorecard) | set(cur.scorecard)):
        change = cur.scorecard.get(key, 0) - prev.scorecard.get(key, 0)
        if change != 0:
            diffs.append(f"{key} {_signed(change)}")
    return diffs


@dataclass
class ScoreHistory:
    """Persists a bounded trail of :class:`RunScore` to the workspace."""

    workspace: Workspace
    path: str = _SCORE_PATH

    def record(self, score: RunScore) -> None:
        """Append ``score`` to the trail (bounded to the most recent runs)."""

        history = self.load()
        history.append(score)
        payload = [vars(s) for s in history[-_MAX_SCORE_RUNS:]]
        self.workspace.write_text(self.path, json.dumps(payload, indent=2))

    def load(self) -> List[RunScore]:
        """Load the recorded runs, oldest first; empty if absent or unreadable."""

        if not self.workspace.exists(self.path):
            return []
        try:
            data = json.loads(self.workspace.read_text(self.path))
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [_run_score_from_dict(d) for d in data if isinstance(d, dict)]

    def latest_delta(self) -> Optional[str]:
        """The most recent run's delta vs the run before it, or ``None``.

        ``None`` when fewer than two runs are recorded, or when nothing tracked
        changed between the last two.
        """

        history = self.load()
        if len(history) < 2:
            return None
        diffs = _score_deltas(history[-2], history[-1])
        return ", ".join(diffs) if diffs else None

    def render(self) -> str:
        """Render the trail as a scoreboard, each run annotated with its delta."""

        history = self.load()
        if not history:
            return "No delivery runs recorded yet."
        lines = [f"Score history ({len(history)} run(s), newest last):"]
        prev: Optional[RunScore] = None
        for score in history:
            headline = (
                f"{'ok' if score.success else 'FAILED'}, "
                f"{score.tasks_succeeded}/{score.tasks_total} tasks, "
                f"{score.total_attempts} attempt(s), ${score.cost_usd:.4f}"
            )
            line = f"- {score.feature}: {headline}"
            if prev is not None:
                diffs = _score_deltas(prev, score)
                if diffs:
                    line += " | delta " + ", ".join(diffs)
            lines.append(line)
            prev = score
        return "\n".join(lines)
