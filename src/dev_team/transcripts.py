"""Opt-in capture of raw agent I/O transcripts to the workspace.

When enabled (it is **off by default**), a :class:`TranscriptRecorder` writes
each agent call's raw system prompt, prompt, and response — plus its cost — to
``<workspace>/.dev_team/transcripts/<run>/<role>-<NNN>.json``. The dashboard's
agent-history modal reads them back through :func:`list_transcripts` /
:func:`read_transcript` so an operator can inspect exactly what each agent was
told and what it said.

Two things to keep in mind about this surface:

- **It is sensitive.** A transcript contains the raw content of the assessed
  repository and the model's verbatim reply. The dashboard is unauthenticated
  and tailnet-only today, so recording is opt-in, each captured field is run
  through a secret-redaction pass (see :func:`_redact`) before it is stored,
  and the read helpers are written as a clearly-delimited guarded surface
  (strict input sanitisation + workspace-membership traversal guard), ready for
  an auth layer to wrap the routes later.
- **It stays bounded.** Each captured field is truncated to ``max_chars`` so a
  runaway prompt/response cannot fill the disk.

Everything goes through the small :class:`~dev_team.execution.Workspace`
interface, so the recorder and the read helpers work unchanged against the
in-memory workspace the tests use — no real filesystem or network required.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable, Dict, List, Optional

from .execution import Workspace, WorkspaceError
from .sdk import AgentResult

#: Workspace-relative directory holding transcripts, laid out as
#: ``<TRANSCRIPTS_DIR>/<run>/<role>-<NNN>.json``.
TRANSCRIPTS_DIR = ".dev_team/transcripts"

#: How many characters of the prompt the list metadata previews.
_PREVIEW_CHARS = 140

#: A run/role token is only ever a plain filename segment.
_FILENAME = re.compile(r"[A-Za-z0-9._-]+")

#: A fixed marker substituted for any secret-shaped span in a captured field.
#: Recording is opt-in and the dashboard is unauthenticated by default, so a
#: transcript can outlive the run and be read by anyone with dashboard access;
#: a secret committed to the assessed repo (or echoed in a prompt/response)
#: must therefore never be persisted here verbatim.
_REDACTED = "[REDACTED]"

#: Secret shapes redacted from every captured field before truncation. Kept
#: deliberately small and specific — high-signal token formats only, so
#: ordinary repository text is not mangled. Each pattern maps its whole match
#: to :data:`_REDACTED`.
_SECRET_PATTERNS = (
    # GitHub fine-grained personal access token.
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    # GitHub classic tokens: PAT (p), OAuth (o), user (u), server (s), refresh (r).
    re.compile(r"gh[pousr]_[A-Za-z0-9]+"),
    # Anthropic API keys.
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    # AWS access key id.
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # PEM-encoded private key blocks (any key type), spanning lines.
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)

#: The ``Authorization: Bearer <token>`` header: keep the header name so the
#: transcript still reads sensibly, redact only the credential that follows.
_BEARER = re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+")


def _redact(text: Optional[str]) -> Optional[str]:
    """Replace secret-shaped spans in ``text`` with :data:`_REDACTED`.

    Applied to every captured field *before* truncation so a secret can never
    be written into a transcript (see :data:`_REDACTED` for why this matters).
    ``None`` passes through untouched.
    """

    if text is None:
        return None
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return _BEARER.sub(r"\g<1>" + _REDACTED, text)


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    """Cap ``text`` at ``limit`` chars, marking how much was dropped."""

    if text is None:
        return None
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]} …[truncated {dropped} chars]"


class TranscriptRecorder:
    """Writes one JSON transcript per agent call into the workspace.

    Constructed per run (its ``run`` id must match the run id the
    :class:`~dev_team.eventlog.EventLog` uses, so the dashboard correlates a
    transcript with the agent's timeline). The delivery engine runs agents
    concurrently, so the per-role sequence counter is guarded by a lock.
    """

    def __init__(
        self,
        workspace: Workspace,
        run: str,
        *,
        clock: Callable[[], float] = time.time,
        max_chars: int = 200_000,
    ) -> None:
        self.workspace = workspace
        self.run = run
        self.clock = clock
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._seqs: Dict[str, int] = {}

    def record(
        self,
        *,
        role: str,
        system_prompt: Optional[str],
        prompt: str,
        result: AgentResult,
    ) -> None:
        """Persist one call's raw I/O as ``<run>/<role>-<NNN>.json``."""

        with self._lock:
            seq = self._seqs.get(role, 0) + 1
            self._seqs[role] = seq
        record = {
            "ts": self.clock(),
            "run": self.run,
            "role": role,
            "seq": seq,
            "system_prompt": _truncate(_redact(system_prompt), self.max_chars),
            "prompt": _truncate(_redact(prompt), self.max_chars),
            "response": _truncate(_redact(result.text), self.max_chars),
            "cost_usd": result.cost_usd,
            "is_error": result.is_error,
        }
        path = f"{TRANSCRIPTS_DIR}/{self.run}/{role}-{seq:03d}.json"
        self.workspace.write_text(path, json.dumps(record))


def _safe_segment(value: object) -> Optional[str]:
    """A run/role token safe to build a path from, or ``None`` if suspicious.

    Rejects anything that is not a plain filename segment — in particular
    ``/``, ``\\`` and ``..`` — *before* any path is constructed. This is the
    first half of the traversal guard; the caller still gates on workspace
    membership.
    """

    if not isinstance(value, str):
        return None
    if not value or ".." in value or "/" in value or "\\" in value:
        return None
    if not _FILENAME.fullmatch(value):
        return None
    return value


def _safe_seq(value: object) -> Optional[str]:
    """Normalise a sequence input to a zero-padded token, or ``None``."""

    text = str(value)
    if not text.isdigit():
        return None
    return f"{int(text):03d}"


def list_transcripts(workspace: Workspace, run: str, role: str) -> List[Dict]:
    """Metadata for every transcript of ``role`` in ``run``, sorted by seq.

    Returns ``[]`` when nothing is recorded (or recording is disabled). Only
    files that are real members of ``workspace.list_files()`` and match the
    sanitised ``<run>/<role>-*.json`` prefix are considered.
    """

    run_s = _safe_segment(run)
    role_s = _safe_segment(role)
    if run_s is None or role_s is None:
        return []
    prefix = f"{TRANSCRIPTS_DIR}/{run_s}/{role_s}-"
    records: List[Dict] = []
    for path in workspace.list_files():
        if not path.startswith(prefix) or not path.endswith(".json"):
            continue
        try:
            data = json.loads(workspace.read_text(path))
        except (OSError, ValueError, WorkspaceError):
            continue
        if not isinstance(data, dict):
            continue
        prompt = str(data.get("prompt") or "")
        records.append(
            {
                "seq": data.get("seq"),
                "ts": data.get("ts"),
                "cost_usd": data.get("cost_usd"),
                "is_error": data.get("is_error"),
                "prompt_preview": prompt[:_PREVIEW_CHARS],
            }
        )
    records.sort(key=lambda r: r["seq"] if isinstance(r["seq"], int) else 0)
    return records


def read_transcript(
    workspace: Workspace, run: str, role: str, seq: object
) -> Optional[Dict]:
    """The full record for one call, or ``None`` if it is not a real member.

    ``run``/``role``/``seq`` are sanitised before the candidate path is built,
    and the file must appear in ``workspace.list_files()`` (the membership
    check *is* the traversal guard, mirroring the dashboard's ``/api/report``).
    """

    run_s = _safe_segment(run)
    role_s = _safe_segment(role)
    seq_s = _safe_seq(seq)
    if run_s is None or role_s is None or seq_s is None:
        return None
    candidate = f"{TRANSCRIPTS_DIR}/{run_s}/{role_s}-{seq_s}.json"
    if candidate not in workspace.list_files():
        return None
    try:
        data = json.loads(workspace.read_text(candidate))
    except (OSError, ValueError, WorkspaceError):
        return None
    if not isinstance(data, dict):
        return None
    return data
