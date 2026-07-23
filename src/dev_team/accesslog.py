"""Persist a bounded audit trail of dispatch HTTP requests.

CLAUDE.md's AI Agent IAM Policy (section 7) requires that "every agent
authentication and call... produce a retained, reviewable log." The dispatch
service (:mod:`dev_team.dispatch`) silences its per-request stderr line
(``Handler.log_message``) and replaces it with nothing, so today it leaves no
trace of who tried what: not a submitted job, not an unauthorised request,
not a hit on an unknown path.

:class:`AccessLog` closes that gap with the same bounded-JSONL-journal
pattern :class:`~dev_team.eventlog.EventLog` already uses for job progress
(``.dev_team/events.jsonl``), applied one layer up: at the HTTP handler
rather than the job runner. Logged fields are deliberately minimal —
``ts``/``method``/``path``/``status``, plus an optional ``job_id`` recorded
only for the ``POST /jobs`` request that created it — and deliberately
exclude the ``Authorization`` header value and any request or response
body, so the log itself can never become a new credential- or payload-leak
surface.

Unlike :class:`~dev_team.eventlog.EventLog`, this journal is not backed by a
:class:`~dev_team.execution.Workspace`: the dispatch service's ``jobs_root``
is a bare directory that may not exist yet (constructing a
:class:`~dev_team.execution.Workspace` over it eagerly would create it as a
side effect of merely building a :class:`~dev_team.dispatch.Dispatcher`, well
before any HTTP request happens). Instead it operates on the root path
directly with raw ``pathlib`` calls — the same choice
:meth:`~dev_team.dispatch.Dispatcher.purge_job` already makes for
``jobs_root`` — and defers any filesystem access, including directory
creation, to the first :meth:`AccessLog.append` call.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: Filename (relative to the dispatch service's jobs root) the access log
#: journal lives at.
ACCESS_LOG_FILENAME = "access.jsonl"

#: Rewrite threshold; the newest ``MAX_ACCESS_RECORDS // 2`` lines survive.
#: Mirrors :data:`dev_team.eventlog.MAX_EVENTS`.
MAX_ACCESS_RECORDS = 4000

#: Maximum bytes of the persisted ``path`` field. The HTTP server itself
#: already caps a whole request line (``http.server._MAXLINE``, 64 KiB), but
#: that is an implementation detail of the stdlib server, not a contract this
#: module should rely on — an explicit, independent bound stops a
#: maximal-length request line from inflating any single log entry
#: disproportionately.
MAX_PATH_BYTES = 2048


def _truncate_path(path: str, limit: int = MAX_PATH_BYTES) -> str:
    """``path`` clipped to ``limit`` UTF-8 bytes, always decoding cleanly.

    Truncating a ``str`` by character count could still produce an
    over-length record for non-ASCII paths; truncating the UTF-8 *bytes* and
    discarding a partial trailing multi-byte sequence (``errors="ignore"``)
    keeps the byte bound exact while always yielding a valid ``str``.
    """

    encoded = path.encode("utf-8", errors="surrogatepass")
    if len(encoded) <= limit:
        return path
    return encoded[:limit].decode("utf-8", errors="ignore")


class AccessLog:
    """Bounded JSONL journal of dispatch HTTP requests.

    Requests are served concurrently (``ThreadingHTTPServer``), so the whole
    read-modify-write append is serialised under an instance lock — the same
    lost-update guard :class:`~dev_team.eventlog.EventLog` uses.

    A filesystem failure (disk full, unwritable root) propagates as an
    ``OSError`` rather than being swallowed here: the dispatch handler
    decides, at the call site, that a log-write failure must never affect a
    response already sent to its caller (see ``dispatch.py``'s
    ``handle_one_request`` override) — swallowing it two layers away, inside
    this class, would hide that decision from the code that actually needs
    to make it.
    """

    def __init__(
        self,
        root: str,
        *,
        clock: Callable[[], float] = time.time,
        filename: str = ACCESS_LOG_FILENAME,
    ) -> None:
        self.root = root
        self.clock = clock
        self.filename = filename
        self._lock = threading.Lock()

    def append(
        self,
        *,
        method: str,
        request_path: str,
        status: int,
        job_id: Optional[str] = None,
    ) -> None:
        """Append one record. Raises ``OSError`` on a filesystem failure."""

        record: Dict[str, Any] = {
            "ts": self.clock(),
            "method": method,
            "path": _truncate_path(request_path),
            "status": status,
        }
        if job_id is not None:
            record["job_id"] = job_id
        target = Path(self.root) / self.filename
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            existing = target.read_text() if target.exists() else ""
            lines = existing.splitlines()
            lines.append(json.dumps(record))
            if len(lines) > MAX_ACCESS_RECORDS:
                lines = lines[-(MAX_ACCESS_RECORDS // 2):]
            # Write-then-rename so a crash mid-write can never leave a
            # truncated file — same discipline as LocalWorkspace.write_text.
            staging = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            staging.write_text("\n".join(lines) + "\n")
            staging.replace(target)


def read_access_log(
    root: str, *, limit: int = 300, filename: str = ACCESS_LOG_FILENAME
) -> List[Dict[str, Any]]:
    """The newest ``limit`` journaled access records, oldest first; never raises."""

    target = Path(root) / filename
    if not target.exists():
        return []
    try:
        text = target.read_text()
    except OSError:
        return []
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records[-limit:]
