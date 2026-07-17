"""An append-only audit trace for observability.

Distinct from ephemeral progress :mod:`~dev_team.events`, the tracer keeps a
durable, ordered record of everything that happened — agent calls, tool runs,
decisions, approvals — so a run can be inspected, audited, or replayed after the
fact. Timestamps are injected (via a clock) to keep it deterministic in tests.

In-process spans alone do not survive process exit; an optional ``sink`` (see
:class:`~dev_team.tracelog.TraceLog`) is how a caller makes the trace durable
on disk, the same seam :mod:`~dev_team.eventlog` uses for progress events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# A clock returns a monotonic-ish float timestamp; injected for determinism.
Clock = Callable[[], float]

# Invoked with a finalised (ended) span; never called for a still-open span.
Sink = Callable[["TraceSpan"], None]


@dataclass
class TraceSpan:
    """A single recorded operation."""

    seq: int
    kind: str
    name: str
    started_at: float
    ended_at: Optional[float] = None
    status: str = "ok"
    attributes: Dict[str, str] = field(default_factory=dict)

    @property
    def duration(self) -> Optional[float]:
        """Elapsed time, or ``None`` if the span is still open."""

        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at


class Tracer:
    """Records an ordered sequence of spans using an injected clock.

    An optional ``sink`` is called with each span exactly once, when it is
    finalised by :meth:`end` — never for a still-open span. ``None`` (the
    default) keeps every existing caller's in-memory-only behaviour unchanged.
    """

    def __init__(self, clock: Optional[Clock] = None, sink: Optional[Sink] = None) -> None:
        self._clock: Clock = clock if clock is not None else _default_clock
        self._sink = sink
        self.spans: List[TraceSpan] = []
        self._seq = 0

    def _now(self) -> float:
        return self._clock()

    def start(self, kind: str, name: str, **attributes: str) -> TraceSpan:
        """Open a new span and return it."""

        span = TraceSpan(
            seq=self._seq,
            kind=kind,
            name=name,
            started_at=self._now(),
            attributes={k: str(v) for k, v in attributes.items()},
        )
        self._seq += 1
        self.spans.append(span)
        return span

    def end(self, span: TraceSpan, status: str = "ok") -> TraceSpan:
        """Close ``span`` with a final ``status``, then notify the sink."""

        span.ended_at = self._now()
        span.status = status
        if self._sink is not None:
            self._sink(span)
        return span

    def event(self, kind: str, name: str, **attributes: str) -> TraceSpan:
        """Record a zero-duration span (a point-in-time event)."""

        span = self.start(kind, name, **attributes)
        return self.end(span)

    def by_kind(self, kind: str) -> List[TraceSpan]:
        """Return all spans of a given ``kind``."""

        return [s for s in self.spans if s.kind == kind]

    def render(self) -> str:
        """Render the trace as newline-separated lines."""

        lines = []
        for span in self.spans:
            duration = "" if span.duration is None else f" {span.duration:.3f}s"
            lines.append(f"#{span.seq} [{span.kind}] {span.name} {span.status}{duration}")
        return "\n".join(lines)


def _default_clock() -> float:
    """Wall-clock seconds; only used when no clock is injected."""

    import time  # pragma: no cover - trivial indirection, faked in tests

    return time.monotonic()  # pragma: no cover
