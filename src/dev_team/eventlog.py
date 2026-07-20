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
import threading
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
        lock: Optional[threading.Lock] = None,
    ) -> None:
        self.workspace = workspace
        self.run = run
        self.clock = clock
        self.path = path
        # The delivery engine runs agents concurrently, so several threads can
        # deliver an event at once. The journal is appended read-modify-write
        # (Workspace has no append primitive), which is a lost-update race: two
        # writers read the same file, each appends its line, and the second
        # write clobbers the first's event. Serialise the whole RMW per log so
        # concurrent deliveries cannot lose each other's events. A caller that
        # shares this file with another read-modify-write (e.g. Dispatcher
        # threading its purge-time `remove_run` call through the same lock)
        # passes its own lock so the two can never interleave; every existing
        # caller omits it and gets a fresh, private Lock() -- byte-identical
        # behaviour to before this parameter existed.
        self._lock = lock if lock is not None else threading.Lock()

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
        with self._lock:
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


def remove_run(
    workspace: Workspace,
    run: str,
    *,
    path: str = EVENTS_PATH,
    lock: Optional[threading.Lock] = None,
) -> int:
    """Drop every journaled line whose ``run`` field equals ``run``.

    Used by purge to permanently remove a job's events from the shared
    journal -- the one piece of purge's deletion `EventLog.__call__`
    doesn't cover, since purge acts on a job id after the job itself has
    finished appending.

    A line that fails to parse as JSON, or parses to something other than
    a dict, is kept as-is: it can't be attributed to ``run``, so dropping
    it risks losing another job's malformed-but-real entry. ``run`` is only
    ever compared against the parsed ``"run"`` field -- never used to build
    a path -- so a path-traversal-shaped id just matches nothing.

    Missing file is a no-op that returns ``0`` without creating one. A file
    that exists but has no matching line is still rewritten (matching
    :class:`EventLog`'s own always-rewrite behaviour), byte-for-byte equal
    to its input, and also returns ``0``.

    ``lock`` should be the same lock instance passed to any :class:`EventLog`
    writing the same ``path``, so a concurrent append and this read-modify-
    write can never interleave and lose an update. Omitted, a fresh private
    lock is used -- safe only when no concurrent writer shares ``path``.
    """

    if lock is None:
        lock = threading.Lock()
    with lock:
        if not workspace.exists(path):
            return 0
        text = workspace.read_text(path)
        kept: List[str] = []
        removed = 0
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if isinstance(record, dict) and record.get("run") == run:
                removed += 1
                continue
            kept.append(line)
        workspace.write_text(path, "\n".join(kept) + "\n")
        return removed


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
