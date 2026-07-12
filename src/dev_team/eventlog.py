"""Persist progress events to the workspace so other processes can watch.

The engines emit :class:`~dev_team.events.AgentEvent` to an in-process
listener; that is enough for ``--verbose`` but invisible to anything else.
:class:`EventLog` is a listener that also appends each event — timestamped
and tagged with a run id — to ``.dev_team/events.jsonl`` in the workspace,
which is what lets the dashboard (a separate process) show what every agent
is doing and what it last worked on.

The log is bounded: when it grows past :data:`MAX_EVENTS` lines it is
rewritten keeping the newest half, so a long-lived workspace never accretes
an unbounded file. Reads are forgiving — a corrupt line (crashed writer,
concurrent append) is skipped, never fatal.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Dict, List, Optional

from .events import AgentEvent, Listener
from .execution import Workspace

#: Workspace-relative path of the event log.
EVENTS_PATH = ".dev_team/events.jsonl"

#: Rewrite threshold; the newest ``MAX_EVENTS // 2`` lines survive.
MAX_EVENTS = 4000


class EventLog:
    """A :data:`~dev_team.events.Listener` that journals events as JSON lines."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        run: str,
        clock: Callable[[], float] = time.time,
        path: str = EVENTS_PATH,
    ) -> None:
        self.workspace = workspace
        self.run = run
        self.clock = clock
        self.path = path

    def __call__(self, event: AgentEvent) -> None:
        record = {
            "ts": self.clock(),
            "run": self.run,
            "role": event.role,
            "stage": event.stage,
            "message": event.message,
            "detail": event.detail,
            "name": event.name,
        }
        existing = ""
        if self.workspace.exists(self.path):
            existing = self.workspace.read_text(self.path)
        lines = existing.splitlines()
        lines.append(json.dumps(record))
        if len(lines) > MAX_EVENTS:
            lines = lines[-(MAX_EVENTS // 2):]
        self.workspace.write_text(self.path, "\n".join(lines) + "\n")


def read_events(
    workspace: Workspace, *, limit: int = 300, path: str = EVENTS_PATH
) -> List[Dict]:
    """The newest ``limit`` journaled events, oldest first; never raises."""

    if not workspace.exists(path):
        return []
    try:
        text = workspace.read_text(path)
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    events: List[Dict] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            events.append(record)
    return events[-limit:]


def compose(*listeners: Optional[Listener]) -> Optional[Listener]:
    """One listener fanning out to every non-``None`` argument.

    Returns ``None`` when nothing is listening, preserving the engines'
    "no listener configured" fast path.
    """

    active = [listener for listener in listeners if listener is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def fan_out(event: AgentEvent) -> None:
        for listener in active:
            listener(event)

    return fan_out
