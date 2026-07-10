"""Progress events emitted by agents and the workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class AgentEvent:
    """A progress event emitted during a development run.

    Attributes:
        role: The role of the agent (or ``"workflow"``) that emitted the event.
        stage: A short machine-friendly stage name (e.g. ``"planning"``).
        message: A human-readable description of what happened.
        detail: Optional extra context (task id, counts, etc.).
    """

    role: str
    stage: str
    message: str
    detail: Optional[str] = None

    def __str__(self) -> str:
        base = f"[{self.role}/{self.stage}] {self.message}"
        if self.detail:
            return f"{base} ({self.detail})"
        return base


# A listener is any callable that receives an :class:`AgentEvent`.
Listener = Callable[[AgentEvent], None]


def emit(listener: Optional[Listener], event: AgentEvent) -> None:
    """Send ``event`` to ``listener`` if one is configured."""

    if listener is not None:
        listener(event)
