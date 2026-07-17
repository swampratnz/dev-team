"""A persisted benchmark trend trail: cross-run aggregate results with deltas.

The delivery score trail (:mod:`dev_team.scores`) tracks *one delivery's*
attempts against a `Workspace`. The benchmark suite (:mod:`dev_team.benchmark`)
has no such durable workspace — each case runs in a throwaway temp directory —
so a regression in its aggregate pass rate or cost is only visible by
manually diffing two CI runs' console logs. :class:`BenchmarkHistory` closes
that gap the same way `ScoreHistory` does for a delivery: a compact, bounded
JSON trail with a run-over-run delta, but over a plain local file rather than
a `Workspace`, since the benchmark harness has none to write into.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

# Bounded history: the trail must not grow without limit across CI runs.
_MAX_HISTORY_RUNS = 50


@dataclass
class BenchmarkRun:
    """The headline metrics of a single benchmark suite run."""

    cases_total: int
    cases_passed: int
    cost_usd: float
    timestamp: str  # ISO 8601, UTC


def _benchmark_run_from_dict(data: Dict[str, Any]) -> BenchmarkRun:
    """Build a :class:`BenchmarkRun` from stored JSON, defensively.

    Mirrors ``dev_team.scores._run_score_from_dict``: each field is read with
    a default so a partial or older record never crashes a load.
    """

    return BenchmarkRun(
        cases_total=data.get("cases_total", 0),
        cases_passed=data.get("cases_passed", 0),
        cost_usd=data.get("cost_usd", 0.0),
        timestamp=str(data.get("timestamp", "")),
    )


def _signed_pct(delta: float) -> str:
    """Render a fraction delta as a signed percentage-point string."""

    pct = delta * 100
    return f"+{pct:.1f}pp" if pct >= 0 else f"-{abs(pct):.1f}pp"


def _signed_cost(delta: float) -> str:
    """Render a cost delta with an explicit sign and dollar sign."""

    return f"+${delta:.4f}" if delta >= 0 else f"-${abs(delta):.4f}"


@dataclass
class BenchmarkHistory:
    """Persists a bounded trail of :class:`BenchmarkRun` to a local JSON file."""

    path: str

    def record(self, run: BenchmarkRun) -> None:
        """Append ``run`` to the trail (bounded to the most recent runs)."""

        history = self.load()
        history.append(run)
        payload = [vars(r) for r in history[-_MAX_HISTORY_RUNS:]]
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2))

    def load(self) -> List[BenchmarkRun]:
        """Load the recorded runs, oldest first; empty if absent or unreadable."""

        target = Path(self.path)
        if not target.is_file():
            return []
        try:
            data = json.loads(target.read_text())
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [_benchmark_run_from_dict(d) for d in data if isinstance(d, dict)]

    def latest_delta(self) -> Optional[str]:
        """The most recent run's pass-rate/cost delta vs the run before it.

        ``None`` when fewer than two runs are recorded.
        """

        history = self.load()
        if len(history) < 2:
            return None
        prev, cur = history[-2], history[-1]
        prev_rate = prev.cases_passed / prev.cases_total if prev.cases_total else 0.0
        cur_rate = cur.cases_passed / cur.cases_total if cur.cases_total else 0.0
        cost_delta = cur.cost_usd - prev.cost_usd
        return (
            f"pass-rate {_signed_pct(cur_rate - prev_rate)}, "
            f"cost {_signed_cost(cost_delta)}"
        )
