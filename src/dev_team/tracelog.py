"""Persist the tracer's audit spans to the workspace, durably.

:class:`~dev_team.trace.Tracer` keeps its spans in process memory only; wired
as its ``sink``, :class:`TraceLog` appends each finalised span — one call, one
agent role, one status, one duration, one cost — as a JSON line to
``.dev_team/trace.jsonl``, closing the CLAUDE.md section 7 gap ("every agent
call must produce a retained, reviewable log") for the CLI's primary
``--deliver``/``--assess`` usage path, which today has no retained record
short of opting into full raw-content transcripts (see
:mod:`~dev_team.transcripts`).

A :class:`~dev_team.trace.TraceSpan` has never carried prompt or response
text — only ``kind``/``name``/``status``/``duration``/``attributes: Dict[str,
str]`` (the latter used for small metadata like ``cost_usd``, never raw
content). So this log is metadata-only *by construction*, not by a redaction
filter that could be bypassed or forgotten, and it is safe to keep always on
rather than gating it behind the transcript recorder's opt-in flag.

Same bounded-JSONL-journal pattern as :class:`~dev_team.eventlog.EventLog`:
bounded (:data:`MAX_TRACE_SPANS`, rewrite keeping the newest half past the
cap) and forgiving on read (a corrupt line is skipped, never fatal). Unlike
:class:`EventLog`, a write failure here is swallowed rather than propagated:
:meth:`Tracer.end` (see :mod:`~dev_team.trace`) calls the sink synchronously
from deep inside the engine's business logic, at many more call sites than
just the instrumented agent call, so letting a logging failure raise there
would break the very run it is meant to audit — the same "recording must
never break a run" contract :class:`~dev_team.transcripts.TranscriptRecorder`
already follows.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Dict, List

from .execution import Workspace, WorkspaceError
from .trace import TraceSpan

#: Workspace-relative path of the trace log.
TRACE_PATH = ".dev_team/trace.jsonl"

#: Rewrite threshold; the newest ``MAX_TRACE_SPANS // 2`` lines survive.
#: Mirrors :data:`dev_team.eventlog.MAX_EVENTS`.
MAX_TRACE_SPANS = 4000


class TraceLog:
    """A :class:`~dev_team.trace.Tracer` sink that journals spans as JSON lines."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        run: str,
        clock: Callable[[], float] = time.time,
        path: str = TRACE_PATH,
    ) -> None:
        self.workspace = workspace
        self.run = run
        self.clock = clock
        self.path = path
        # Spans can be ended from concurrently-running agent tasks (the
        # delivery engine runs specialists concurrently); serialise the
        # read-modify-write append exactly like EventLog does, for the same
        # lost-update reason.
        self._lock = threading.Lock()

    def __call__(self, span: TraceSpan) -> None:
        """Append ``span``. Never raises — a write failure is swallowed."""

        record = {
            "ts": self.clock(),
            "run": self.run,
            "seq": span.seq,
            "kind": span.kind,
            "name": span.name,
            "status": span.status,
            "duration": span.duration,
            "attributes": span.attributes,
        }
        try:
            with self._lock:
                existing = ""
                if self.workspace.exists(self.path):
                    existing = self.workspace.read_text(self.path)
                lines = existing.splitlines()
                lines.append(json.dumps(record))
                if len(lines) > MAX_TRACE_SPANS:
                    lines = lines[-(MAX_TRACE_SPANS // 2):]
                self.workspace.write_text(self.path, "\n".join(lines) + "\n")
        except (OSError, WorkspaceError, ValueError):
            pass


def read_trace_log(
    workspace: Workspace, *, limit: int = 300, path: str = TRACE_PATH
) -> List[Dict]:
    """The newest ``limit`` journaled spans, oldest first; never raises."""

    if not workspace.exists(path):
        return []
    try:
        text = workspace.read_text(path)
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    spans: List[Dict] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            spans.append(record)
    return spans[-limit:]
