"""An authenticated HTTP dispatch service over the :class:`DevTeam` facade.

``dev-team --dispatch`` exposes a small, bearer-authenticated HTTP API so an
external tailnet caller (a bot) can drive the team remotely: SUBMIT a job
(``assess`` or ``deliver`` against a repository, or ``verify`` to have a
fresh skeptical agent re-check one persisted assessment finding), poll its
STATUS, and fetch the RESULT. It wraps exactly the same code paths the CLI's ``--assess`` /
``--deliver`` modes use — clone the repo, build a :class:`DevTeam`, and run
:meth:`DevTeam.assess` / :meth:`DevTeam.deliver`.

The design mirrors :mod:`dev_team.dashboard`: stdlib
:class:`~http.server.ThreadingHTTPServer`, no dependencies, a request handler
class bound to a core object, and per-request stderr silenced. Four
guardrails matter for a service that holds Claude credentials and runs agent
code on a shared box:

- **Auth.** Every route except ``GET /health`` requires
  ``Authorization: Bearer <token>``; the token is compared with
  :func:`hmac.compare_digest` (constant-time) and a miss is ``401``.
- **Single-flight.** A background worker thread runs its own asyncio event
  loop and drains a queue **one job at a time**. The box has one shared Claude
  subscription and dev-team has no cross-run locking, so overlapping runs would
  corrupt each other — the queue serialises them.
- **Tailnet-bound.** The CLI binds the unit to the tailnet IP only (see
  ``deploy/dev-team-dispatch.service``); nothing here is exposed to the public
  internet.
- **Access-logged.** ``Handler.log_message`` silences the stdlib's default
  per-request stderr line, but every request — ``/health``, authorised,
  ``401``, and unknown-path ``404`` alike — still appends exactly one record
  (method, path, status; never the bearer token or a request/response body)
  to a bounded journal via :class:`~dev_team.accesslog.AccessLog`, so a
  suspected-compromised token or an unrecognised-path probe leaves a
  retained, reviewable trace (CLAUDE.md section 7).

The seams the constructor exposes (``runner``, ``materialise``, ``clock``,
``jobs_root``) exist so the whole executor can run offline in tests with an
injected fake runner and a fake materialise — never touching Claude or the
network — exactly as ``test_cli.py`` / ``test_team.py`` inject a fake runner.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .accesslog import AccessLog
from .approval import PolicyApprovalGate
from .assessment import (
    AssessConfig,
    calibration_summary,
    dict_to_backlog,
    find_finding,
    list_findings,
    outcome_to_dict,
    verify_finding,
)
from .backlog import (
    Backlog,
    BacklogStore,
    ItemStatus,
    Story,
    _story_to_dict,
    validate_dependencies,
)
from .budget import Budget
from .config import TeamConfig
from .engine import EngineConfig
from .errors import DependencyCycleError, DevTeamError
from .eventlog import EventLog, compose, read_events
from .execution import (
    LocalWorkspace,
    SubprocessCommandRunner,
    Workspace,
    WorkspaceError,
)
from .interaction import QueueChannel, Question, Reply
from .models import FeatureRequest
from .report import delivery_to_dict
from .sdk import AgentRunner
from .sources import (
    SourceError,
    clone_or_update,
    default_env_file,
    parse_repo,
    resolve_github_token,
)
from .team import DevTeam
from .transcripts import TranscriptRecorder

#: Default port for the dispatch service (the dashboard keeps 8737).
DEFAULT_PORT = 8738

#: Default root under which each job's clone/workspace is created.
DEFAULT_JOBS_ROOT = "/opt/dev-team/jobs"

#: Reject a SUBMIT once this many jobs are already waiting (queued).
DEFAULT_QUEUE_CAP = 16

#: Upper bound on a POST body; a larger Content-Length is rejected unread (a
#: SUBMIT body is a small JSON object, so 1 MiB is generous headroom). Without
#: it an oversized or lying Content-Length would have us buffer the whole
#: request into memory — an unauthenticated-adjacent resource-exhaustion path.
_MAX_BODY = 1 << 20

#: How many of the newest jobs ``GET /jobs`` lists by default, the hard
#: ceiling a caller-supplied ``?limit=`` is clamped to, and how many progress
#: events ``GET /jobs/{id}`` carries.
_LIST_LIMIT = 25
_LIST_LIMIT_MAX = 100
_PROGRESS_LIMIT = 12

#: Wall-clock ceiling for a single job. The single-flight worker runs one job
#: at a time, so a job that hangs (a stuck clone, an agent session that never
#: returns) would wedge the queue forever; the worker aborts it past this bound
#: and moves on. Injectable via the ``job_timeout`` constructor param for tests.
_JOB_TIMEOUT_SECONDS = 3600.0

#: Default :class:`~dev_team.interaction.QueueChannel` timeout (seconds) an
#: interactive deliver job's ``interactive_timeout_seconds`` resolves to when
#: omitted, and the floor/ceiling every requested value is clamped to before
#: use — mirrors #71's poll-timeout clamp, for the same reason: a
#: misconfigured or malicious huge timeout must not wedge the single-flight
#: worker on one paused job.
_INTERACTIVE_TIMEOUT_DEFAULT = 300.0
_INTERACTIVE_TIMEOUT_FLOOR = 30.0
_INTERACTIVE_TIMEOUT_CEILING = 1800.0

#: The run modes the service accepts. ``verify`` re-checks one finding from
#: a previously mirrored assessment against a fresh clone of its repository.
_MODES = ("assess", "deliver", "verify")

#: Terminal job states.
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})

#: The exact ``audit/<id>/`` files a purge removes, each through
#: :meth:`Workspace.delete` (never a raw filesystem call against the
#: dashboard workspace — see :meth:`Dispatcher.purge_job`).
_AUDIT_FILES = ("assessment.md", "assessment.json", "meta.json", "verifications.jsonl")

# A sentinel the worker loop treats as "stop draining and exit".
_SHUTDOWN = object()


class ValidationError(Exception):
    """A bad SUBMIT body — surfaced to the client as ``400``."""


class QueueFull(Exception):
    """The pending queue is at capacity — surfaced as ``503``."""


class SubmitRejected(Exception):
    """A verify SUBMIT that fails against persisted state, with its status.

    Distinct from :class:`ValidationError` (always 400): a verify request can
    be well-formed yet name an assessment that was never mirrored (404), a
    finding that resolves to nothing (404), or a service with no dashboard
    workspace to read from (409).
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class JobSpec:
    """A validated request to run one job (id assigned at submit time)."""

    mode: str
    repo: str
    title: str
    description: str
    budget_usd: Optional[float]
    backlog: bool = False
    # Meaningful only for mode: "deliver" (accepted but ignored on assess /
    # verify, mirroring how backlog is validated for both modes but only
    # applied to assess). interactive_timeout_seconds is the RAW requested
    # value (or None); _resolved_interactive_timeout clamps/defaults it at
    # the point a _TrackedChannel is actually constructed.
    interactive: bool = False
    interactive_timeout_seconds: Optional[float] = None
    id: str = ""
    # verify only: the source assess job, the resolved finding id, and the
    # RESOLVED finding itself (resolved synchronously at submit time so the
    # job cannot start and then discover the finding never existed).
    source_job: Optional[str] = None
    finding_id: Optional[str] = None
    finding: Optional[Dict[str, Any]] = None
    # verify only: skip the agent call entirely (see
    # dev_team.assessment.verify_finding) when the finding is already known,
    # at $0, to cite a broken/fabricated evidence path.
    skip_broken_citations: bool = False


@dataclass
class JobRecord:
    """A job's live state in the in-memory registry."""

    spec: JobSpec
    state: str = "queued"
    started: Optional[float] = None
    ended: Optional[float] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    outcome: Any = None
    workspace: Optional[Workspace] = None
    # The run's :class:`~dev_team.budget.Budget`, attached by ``run_job`` as
    # soon as it is created so a job that fails (or is timed out) mid-run can
    # report the partial spend it already banked, rather than a hard 0.0. Stays
    # ``None`` for a failure before the budget exists (e.g. a clone that raised).
    budget: Optional[Budget] = None
    # Set by run_job only for an interactive deliver job (mode == "deliver"
    # and spec.interactive); stays None otherwise, which is exactly what
    # GET .../question / POST .../answer treat as "never interactive".
    channel: Optional["_TrackedChannel"] = None


def _resolved_interactive_timeout(requested: Optional[float]) -> float:
    """The clamped timeout an interactive deliver job's channel actually uses.

    ``requested`` is the raw ``JobSpec.interactive_timeout_seconds`` (already
    type-validated at submit time in :meth:`Dispatcher.build_spec`); ``None``
    resolves to :data:`_INTERACTIVE_TIMEOUT_DEFAULT` before clamping to
    ``[_INTERACTIVE_TIMEOUT_FLOOR, _INTERACTIVE_TIMEOUT_CEILING]`` so an
    out-of-range request (too small to be useful, or large enough to wedge
    the single-flight worker) is silently bounded rather than stored/used
    as-is.
    """

    value = _INTERACTIVE_TIMEOUT_DEFAULT if requested is None else float(requested)
    return max(_INTERACTIVE_TIMEOUT_FLOOR, min(_INTERACTIVE_TIMEOUT_CEILING, value))


@dataclass
class _TrackedChannel(QueueChannel):
    """A :class:`~dev_team.interaction.QueueChannel` that exposes its pending
    question and answers it race-free.

    :attr:`QueueChannel.replies` is one queue shared for the channel's whole
    lifetime — reusing it here would let a reply meant for one ``ask()`` call
    be validated against that question but actually delivered (a moment
    later, after the validating lock is released) to a *different* question
    the engine has since moved on to, e.g. once ``ask()`` has already timed
    out and taken the default. Instead, :meth:`ask` mints a fresh
    single-slot queue for each question and publishes ``(question, slot)``
    together with :attr:`current` under :attr:`_answer_lock`; :meth:`submit_reply`
    validates a choice against — and delivers it to — that exact ``(question,
    slot)`` pair atomically under the same lock, so a choice can never be
    handed to a question other than the live one it was validated against.

    :attr:`current` remains for ``GET /jobs/{id}/question``'s non-destructive
    peek; it is set alongside the pending slot and cleared in a ``finally``
    once answered or timed out.
    """

    current: Optional[Question] = None
    _pending: Optional[Tuple[Question, "queue.Queue[Reply]"]] = field(
        default=None, repr=False, compare=False
    )
    _answer_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def ask(self, question: Question) -> Reply:
        reply_slot: "queue.Queue[Reply]" = queue.Queue(maxsize=1)
        with self._answer_lock:
            self.current = question
            self._pending = (question, reply_slot)
        self.questions.put(question)
        try:
            return reply_slot.get(timeout=self.timeout)
        except queue.Empty:
            return Reply(choice=question.default.key)
        finally:
            with self._answer_lock:
                self.current = None
                self._pending = None

    def submit_reply(self, choice: str, text: str) -> Optional[bool]:
        """Atomically validate and deliver a reply to the live ``ask()`` call.

        Returns ``None`` if there is no live question right now, ``False``
        if ``choice`` isn't among the live question's keys, ``True`` once the
        reply has been handed to the exact ``ask()`` call that posted that
        question. The validate-then-deliver happens under :attr:`_answer_lock`
        — the same lock :meth:`ask` uses to publish/clear the pending
        ``(question, slot)`` pair — so a question can never time out and be
        replaced by a different one between validation and delivery.
        """

        with self._answer_lock:
            pending = self._pending
            if pending is None:
                return None
            question, reply_slot = pending
            if question.find(choice) is None:
                return False
            reply_slot.put(Reply(choice=choice, text=text))
            return True


def _failed_cost(record: JobRecord) -> float:
    """The real spend to attribute to a job that failed or was aborted.

    A job that burned budget before raising (or being timed out) reports the
    partial spend banked on its :class:`~dev_team.budget.Budget`; one that died
    before the budget was ever created — a clone failure, or a timeout during
    materialise — correctly reports ``0.0``.
    """

    return record.budget.spent if record.budget is not None else 0.0


def _default_materialise(spec: JobSpec, dest: str) -> Workspace:
    """Clone (or fast-forward) ``spec.repo`` into ``dest`` — the real path.

    Mirrors the CLI's ``_materialise_repo``: resolve the ref, find the GitHub
    token via the default env-file search, take it *out of* the process
    environment, and clone header-authenticated. Returns a workspace rooted at
    the clone.
    """

    ref = parse_repo(spec.repo)
    token = resolve_github_token(default_env_file())
    clone_or_update(ref, dest, runner=SubprocessCommandRunner(), token=token)
    return LocalWorkspace(dest)


class Dispatcher:
    """The job registry + single-flight worker behind the HTTP layer.

    Directly unit-testable without a socket: :meth:`build_spec` validates a
    SUBMIT body, :meth:`submit` enqueues, and the worker thread (started by
    :meth:`start`) drains the queue one job at a time through
    :meth:`run_job`.
    """

    def __init__(
        self,
        *,
        token: str,
        runner: Optional[AgentRunner] = None,
        materialise: Optional[Callable[[JobSpec, str], Workspace]] = None,
        clock: Callable[[], float] = time.time,
        jobs_root: str = DEFAULT_JOBS_ROOT,
        queue_cap: int = DEFAULT_QUEUE_CAP,
        dashboard_workspace: Optional[Workspace] = None,
        record_transcripts: bool = False,
        job_timeout: float = _JOB_TIMEOUT_SECONDS,
    ) -> None:
        self.token = token
        self._runner = runner
        self._materialise = materialise or _default_materialise
        self._clock = clock
        self._jobs_root = jobs_root
        self._job_timeout = job_timeout
        # Constructing this touches no filesystem state (see AccessLog's
        # docstring) — safe to do unconditionally here even though most
        # Dispatcher instances (every non-HTTP unit test) never end up
        # appending to it.
        self.access_log = AccessLog(jobs_root, clock=clock)
        # Off by default: capturing raw agent I/O is opt-in (the operator
        # enables it via --record-transcripts or DEV_TEAM_RECORD_TRANSCRIPTS).
        self._record_transcripts = record_transcripts
        # Optional shared workspace the standing `--dashboard` process watches:
        # when set, every job ALSO journals its events here (same run id, so it
        # shows as its own run/agent-cards on the dashboard) and an assess run
        # mirrors its report AND structured result under `audit/<id>/` — the
        # JSON is what POST /jobs/{id}/backlog reads later. The job's OWN
        # workspace stays the source of truth and isolated; the backlog merge
        # is the one deliberate exception (the dashboard workspace owns the
        # cross-job backlog).
        self._dashboard_workspace = dashboard_workspace
        self._queue_cap = queue_cap
        self._registry: Dict[str, JobRecord] = {}
        self._order: List[str] = []
        self._events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # Serialises every load→mutate→save of the dashboard workspace's
        # backlog.json. The registry lock above cannot do this job: the
        # worker thread (assess --backlog merge) and the handler threads
        # (make_backlog + the /backlog mutation API) all read-modify-write
        # the same file, and unserialised writers lose each other's updates.
        self._backlog_lock = threading.Lock()
        # Serialises every load→mutate→save of a job's audit/<id>/meta.json
        # archived marker. The worker thread writes meta.json exactly once,
        # via _mirror_meta, while the job's registry state is still
        # "running" (before _execute flips it to a terminal state) — and
        # archive_job refuses a "running"/"queued" job below, so the worker
        # and this lock's critical section never race each other. What this
        # lock actually guards is concurrent archive/unarchive HTTP calls
        # against the same job racing each other's read-modify-write.
        self._meta_lock = threading.Lock()
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._seq = 0
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Spawn the single-flight worker thread (idempotent)."""

        if self._thread is None:
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Signal the worker to drain-and-exit and join it."""

        if self._thread is not None:
            self._queue.put(_SHUTDOWN)
            self._thread.join(timeout=10)
            self._thread = None

    def _worker_loop(self) -> None:
        """Own an asyncio loop and run queued jobs strictly one at a time.

        Each job runs under a wall-clock ceiling (:attr:`_job_timeout`): a job
        that hangs would otherwise wedge the single-flight queue forever, so on
        timeout the worker aborts it, marks it failed, and moves on to the next
        job. ``asyncio.wait_for`` can only unwind the coroutine at an ``await``
        point — a job blocked in synchronous CPU/IO is not interruptible — but
        the stuck cases this guards (an agent session or clone that never
        returns) are all awaiting.
        """

        loop = asyncio.new_event_loop()
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN:
                    return
                try:
                    loop.run_until_complete(
                        asyncio.wait_for(self._execute(item), timeout=self._job_timeout)
                    )
                except asyncio.TimeoutError:
                    self._fail_timed_out(item)
        finally:
            loop.close()

    def _fail_timed_out(self, job_id: str) -> None:
        """Mark a job failed after it blew the wall-clock ceiling.

        ``wait_for`` cancels the ``_execute`` coroutine on timeout, so its own
        completion bookkeeping (the terminal-state flip and the completion
        event) never runs — this does both here instead, attributing whatever
        the run had already spent (:func:`_failed_cost`).
        """

        record = self._registry[job_id]
        with self._lock:
            record.state = "failed"
            record.error = (
                f"job exceeded the {self._job_timeout:g}s time limit and was aborted"
            )
            record.cost_usd = _failed_cost(record)
            record.ended = self._clock()
        self._events[job_id].set()

    # -- submit / query ------------------------------------------------------

    def build_spec(self, body: Dict[str, Any]) -> JobSpec:
        """Validate a SUBMIT body into a :class:`JobSpec` (raises on bad input).

        Raises:
            ValidationError: any contract violation (→ HTTP 400).
        """

        mode = body.get("mode")
        if mode not in _MODES:
            raise ValidationError("mode must be 'assess', 'deliver' or 'verify'")
        budget = body.get("budget_usd")
        if budget is not None:
            if isinstance(budget, bool) or not isinstance(budget, (int, float)):
                raise ValidationError("budget_usd must be a number or null")
            if budget <= 0:
                raise ValidationError("budget_usd must be greater than 0")
        if mode == "verify":
            return self._verify_spec(body, budget)
        repo = body.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise ValidationError("repo is required")
        try:
            ref = parse_repo(repo)
        except (SourceError, ValueError) as exc:
            raise ValidationError(f"invalid repo: {exc}")
        backlog = body.get("backlog", False)
        if not isinstance(backlog, bool):
            raise ValidationError("backlog must be true or false")
        interactive = body.get("interactive", False)
        if not isinstance(interactive, bool):
            raise ValidationError("interactive must be true or false")
        interactive_timeout = body.get("interactive_timeout_seconds")
        if interactive_timeout is not None:
            if isinstance(interactive_timeout, bool) or not isinstance(
                interactive_timeout, (int, float)
            ):
                raise ValidationError(
                    "interactive_timeout_seconds must be a number or null"
                )
        title = body.get("title")
        description = body.get("description")
        if mode == "deliver":
            if not isinstance(title, str) or not title.strip():
                raise ValidationError("deliver requires a non-empty title")
            if not isinstance(description, str) or not description.strip():
                raise ValidationError("deliver requires a non-empty description")
        else:  # assess: title defaults to the repo slug, description to ""
            if not isinstance(title, str) or not title.strip():
                title = ref.slug
            if not isinstance(description, str):
                description = ""
        return JobSpec(
            mode=mode,
            repo=repo,
            title=title,
            description=description,
            budget_usd=budget,
            backlog=backlog,
            interactive=interactive,
            interactive_timeout_seconds=interactive_timeout,
        )

    def _exists(self, path: str) -> bool:
        """Whether ``path`` exists in the dashboard workspace, failing closed.

        A path the workspace refuses to resolve (traversal, absolutes — job
        ids come off the URL/body) simply does not exist; the caller answers
        404 instead of leaking a stack trace.
        """

        try:
            return self._dashboard_workspace.exists(path)
        except WorkspaceError:
            return False

    def _verify_spec(self, body: Dict[str, Any], budget: Optional[float]) -> JobSpec:
        """Validate a verify SUBMIT against the persisted assessment on disk.

        Synchronous on purpose: the caller learns at submit time that the
        source assessment or the finding does not exist, instead of queueing
        a job doomed to fail. Disk-keyed (never the in-memory registry), so
        it works for source jobs that ran before a service restart. The
        repository to re-clone comes from ``audit/<source>/meta.json``, the
        finding from ``audit/<source>/assessment.json``.

        Raises:
            ValidationError: missing/blank ``source_job``/``finding_id``
                (→ 400).
            SubmitRejected: no dashboard workspace to read from (409); the
                source assessment/meta was never mirrored, or the finding
                resolves to nothing (404).
        """

        source_job = body.get("source_job")
        if not isinstance(source_job, str) or not source_job.strip():
            raise ValidationError("verify requires a source_job")
        finding_id = body.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ValidationError("verify requires a finding_id")
        skip_broken_citations = body.get("skip_broken_citations", False)
        if not isinstance(skip_broken_citations, bool):
            raise ValidationError("skip_broken_citations must be true or false")
        source_job = source_job.strip()
        if self._dashboard_workspace is None:
            raise SubmitRejected(409, "verify needs a dashboard workspace")
        assessment_path = f"audit/{source_job}/assessment.json"
        meta_path = f"audit/{source_job}/meta.json"
        if not self._exists(assessment_path) or not self._exists(meta_path):
            # meta.json is written beside every mirrored assessment since
            # verify landed; a job missing either file predates the feature
            # (re-assess to record it) or never assessed at all.
            raise SubmitRejected(404, "no assessment for that job")
        # Corrupt on-disk state (a half-written or hand-edited mirror) must
        # surface as a controlled 404, never an unhandled 500.
        try:
            data = json.loads(self._dashboard_workspace.read_text(assessment_path))
        except (OSError, ValueError, WorkspaceError):
            raise SubmitRejected(404, "no assessment for that job")
        finding = find_finding(data, finding_id.strip())
        if finding is None:
            raise SubmitRejected(404, "finding not found")
        try:
            meta = json.loads(self._dashboard_workspace.read_text(meta_path))
        except (OSError, ValueError, WorkspaceError):
            raise SubmitRejected(404, "no assessment for that job")
        return JobSpec(
            mode="verify",
            repo=str(meta.get("repo", "")),
            title=f"verify {finding['id']}",
            description="",
            budget_usd=budget,
            source_job=source_job,
            finding_id=finding["id"],
            finding=finding,
            skip_broken_citations=skip_broken_citations,
        )

    def submit(self, spec: JobSpec) -> Tuple[str, int]:
        """Register ``spec`` as queued and enqueue it; return ``(id, position)``.

        ``position`` is how many jobs are queued ahead of this one (0 means it
        starts as soon as the worker is free).

        Raises:
            QueueFull: the pending queue is at capacity (→ HTTP 503).
        """

        with self._lock:
            queued = sum(1 for r in self._registry.values() if r.state == "queued")
            if queued >= self._queue_cap:
                raise QueueFull()
            self._seq += 1
            job_id = f"{spec.mode}-{time.strftime('%Y%m%d-%H%M%S')}-{self._seq}"
            spec.id = job_id
            self._registry[job_id] = JobRecord(spec=spec)
            self._order.append(job_id)
            self._events[job_id] = threading.Event()
        self._queue.put(job_id)
        return job_id, queued

    def get(self, job_id: str) -> Optional[JobRecord]:
        """The record for ``job_id`` (or ``None``)."""

        with self._lock:
            return self._registry.get(job_id)

    def recent(
        self,
        *,
        limit: int = _LIST_LIMIT,
        offset: int = 0,
        include_archived: bool = False,
    ) -> List[JobRecord]:
        """A newest-first page of records.

        ``limit`` defaults to :data:`_LIST_LIMIT` and is clamped to
        ``[1, _LIST_LIMIT_MAX]``; ``offset`` (how many newest records to skip)
        is clamped to ``>= 0``. The defaults reproduce the historical
        behaviour — the newest :data:`_LIST_LIMIT` jobs — so callers that pass
        neither are unaffected. Clamping here (not just at the HTTP edge) keeps
        the slice sane for every caller. Archived jobs (per their mirrored
        ``meta.json``) are excluded by default; ``include_archived=True``
        (``GET /jobs?archived=1``) reveals them too.
        """

        limit = max(1, min(limit, _LIST_LIMIT_MAX))
        offset = max(0, offset)
        with self._lock:
            ordered = [self._registry[j] for j in self._order]
        records = list(reversed(ordered))
        if not include_archived:
            records = [r for r in records if not self._is_archived(r.spec.id)]
        return records[offset : offset + limit]

    def wait(self, job_id: str, timeout: float = 5.0) -> bool:
        """Block until ``job_id`` reaches a terminal state (test aid)."""

        event = self._events.get(job_id)
        if event is None:
            return False
        return event.wait(timeout)

    # -- execution -----------------------------------------------------------

    async def _execute(self, job_id: str) -> None:
        """Move a job through ``running`` → ``succeeded`` / ``failed``."""

        record = self._registry[job_id]
        with self._lock:
            if record.state == "cancelled":
                # Cancelled while queued (cancel_job shares this lock) —
                # never ran, no cost. Whichever call wins the race decides
                # the outcome deterministically; no re-set of the
                # completion event needed, cancel_job already set it.
                return
            record.state = "running"
            record.started = self._clock()
        try:
            outcome, cost = await self.run_job(record)
        except Exception as exc:  # noqa: BLE001 — a failed job must not kill the worker
            with self._lock:
                record.state = "failed"
                record.error = str(exc)
                record.cost_usd = _failed_cost(record)
                record.ended = self._clock()
        else:
            with self._lock:
                record.outcome = outcome
                record.cost_usd = cost
                record.state = "succeeded"
                record.ended = self._clock()
        self._events[job_id].set()

    async def run_job(self, record: JobRecord) -> Tuple[Any, float]:
        """Clone, build a :class:`DevTeam`, and run the assess/deliver path.

        Returns ``(outcome, cost_usd)``. Progress is journalled into the job's
        workspace via :class:`EventLog`, so :meth:`status` can surface it.
        """

        spec = record.spec
        dest = str(Path(self._jobs_root) / spec.id)
        workspace = self._materialise(spec, dest)
        record.workspace = workspace
        if spec.mode == "verify":
            # No DevTeam: one fresh skeptical agent re-checks one claim.
            return await self._run_verify(record, spec, workspace)
        # The job's own workspace is always journalled (drives GET /jobs/{id}
        # progress). When a dashboard workspace is configured, fan the same
        # events out to it too — same run id, so the standing dashboard shows
        # this job as its own run without ever touching the isolated job dir.
        listener = EventLog(workspace, run=spec.id, clock=self._clock)
        if self._dashboard_workspace is not None:
            listener = compose(
                listener,
                EventLog(self._dashboard_workspace, run=spec.id, clock=self._clock),
            )
        # Interactive plan review / re-plan supervision / failure escalation
        # is opt-in and deliver-only: a queued/non-interactive job (the
        # default) still gets interaction=None exactly as before, so no
        # _TrackedChannel is ever constructed and behaviour is unchanged.
        interaction = None
        if spec.mode == "deliver" and spec.interactive:
            record.channel = _TrackedChannel(
                timeout=_resolved_interactive_timeout(spec.interactive_timeout_seconds)
            )
            interaction = record.channel
        team = DevTeam(
            self._runner,
            config=TeamConfig(),
            listener=listener,
            interaction=interaction,
        )
        budget = Budget(limit_usd=spec.budget_usd)
        # Attach the budget to the record immediately, so a failure (or a
        # worker timeout) partway through the run can report the spend already
        # banked instead of a hard 0.0 (see _failed_cost).
        record.budget = budget
        # When enabled, transcripts land where the dashboard can read them: the
        # shared dashboard workspace when configured (same place its events are
        # mirrored), else the job's own workspace. Same run id as the events.
        kwargs: Dict[str, Any] = {}
        if self._record_transcripts:
            target = self._dashboard_workspace or workspace
            kwargs["transcript_recorder"] = TranscriptRecorder(target, run=spec.id)
        if spec.mode == "assess":
            outcome = await team.assess(
                workspace=workspace,
                budget=budget,
                config=AssessConfig(update_backlog=spec.backlog),
                **kwargs,
            )
            self._mirror_report(spec.id, outcome)
            self._mirror_assessment_json(spec.id, outcome)
            self._mirror_meta(spec)
            if spec.backlog and self._dashboard_workspace is not None:
                # Merge, don't copy: the dashboard workspace accumulates
                # stories across many jobs, and copying the job's own
                # .dev_team/backlog.json over it would clobber that history.
                # Running the same dict transform against the dashboard's
                # backlog reuses the dedup-by-title logic — exactly what
                # POST /jobs/{id}/backlog does later — so the panel shows
                # the stories immediately and a re-assess refreshes instead
                # of flooding. repo/source_job give the stories a
                # per-repository epic and finding provenance.
                self._merge_backlog(
                    outcome_to_dict(outcome), repo=spec.repo, source_job=spec.id
                )
        else:
            outcome = await team.deliver(
                FeatureRequest(title=spec.title, description=spec.description),
                workspace=workspace,
                budget=budget,
                config=EngineConfig(commit=True),
                # Unattended delivery must not silently push/deploy/rm: gate
                # high-risk commands behind an explicit human approval instead
                # of the engine's default no-op AutoApprover. The risk="medium"
                # feature commit is still auto-approved, so dispatch delivery
                # keeps committing — it just cannot escalate to a push/deploy
                # on its own (CLAUDE.md sections 1, 6, 8).
                approval=PolicyApprovalGate(block_risks=("high",)),
                **kwargs,
            )
        return outcome, outcome.cost_usd

    async def _run_verify(
        self, record: JobRecord, spec: JobSpec, workspace: Workspace
    ) -> Tuple[Any, float]:
        """Re-check the resolved finding against the fresh clone of its repo.

        A FRESH skeptical agent (see :func:`~dev_team.assessment.verify_finding`)
        with read-only tools; the verdict is appended to the SOURCE job's
        ``verifications.jsonl`` in the dashboard workspace, so
        ``GET /jobs/{source}/verifications`` survives a restart exactly like
        the mirrored assessment JSON. An agent failure is raised so the
        worker records a failed job (and ``result()`` answers
        ``{"kind":"verify","success":false,…}``).
        """

        # verify_finding drives the agent DIRECTLY, not through DevTeam, so it
        # needs a concrete runner. In production self._runner is None (the real
        # SDK runner is created lazily inside DevTeam); resolve it the same way
        # the assess/deliver branch does — DevTeam(runner).runner returns the
        # injected runner in tests and a real ClaudeAgentRunner otherwise. Done
        # unconditionally (no `or`) so there is no in-dispatch branch to leave
        # uncovered; the None->real fallback lives in DevTeam, already tested.
        runner = DevTeam(self._runner, config=TeamConfig()).runner
        # Attach the budget to the record before the agent runs, so a failure
        # partway through reports partial spend, not a hard 0.0 (see run_job).
        budget = Budget(limit_usd=spec.budget_usd)
        record.budget = budget
        result = await verify_finding(
            runner,
            workspace,
            spec.finding,
            budget=budget,
            source_job=spec.source_job,
            skip_broken_citations=spec.skip_broken_citations,
        )
        if not result["success"]:
            raise DevTeamError(str(result["error"]))
        if not result.get("skipped"):
            # A $0 skip never invoked a model, so it must never be appended
            # here — GET /calibration's confirm_rate would otherwise be
            # silently diluted by an entry no model actually adjudicated.
            self._mirror_verification(
                spec.source_job,
                {
                    "finding_id": result["finding_id"],
                    "verdict": result["verdict"],
                    "rationale": result["rationale"],
                    "citations": result["citations"],
                    "cost_usd": result["cost_usd"],
                    "ts": self._clock(),
                },
            )
        return result, float(result["cost_usd"])

    def _mirror_report(self, job_id: str, outcome: Any) -> None:
        """Copy an assess run's report into the dashboard workspace.

        Written under a per-job `audit/<id>/` path so concurrent history never
        collides and the dashboard's Reports panel attributes it. No-op when no
        dashboard workspace is configured or the run produced no report.
        """

        if self._dashboard_workspace is None:
            return
        report = getattr(outcome, "report_markdown", None)
        if not report:
            return
        self._dashboard_workspace.write_text(f"audit/{job_id}/assessment.md", report)

    def _mirror_assessment_json(self, job_id: str, outcome: Any) -> None:
        """Persist the structured assess result into the dashboard workspace.

        ``audit/<id>/assessment.json`` (the exact ``outcome_to_dict`` shape)
        is the disk-keyed record :meth:`make_backlog` reads later. The
        in-memory registry is lost on restart, so the later-backlog path must
        never depend on it — this file is what makes ``POST
        /jobs/{id}/backlog`` restart-safe. No-op when no dashboard workspace
        is configured (mirrors :meth:`_mirror_report`).
        """

        if self._dashboard_workspace is None:
            return
        self._dashboard_workspace.write_text(
            f"audit/{job_id}/assessment.json",
            json.dumps(outcome_to_dict(outcome), indent=2),
        )

    def _mirror_meta(self, spec: JobSpec) -> None:
        """Persist the job's repo identity beside its mirrored assessment.

        ``audit/<id>/meta.json`` is what a later ``verify`` submit reads to
        know WHICH repository to re-clone. The in-memory registry also knows
        the repo but is lost on restart, so verify must key off disk — same
        rule as :meth:`make_backlog`. No-op without a dashboard workspace
        (mirrors :meth:`_mirror_assessment_json`).
        """

        if self._dashboard_workspace is None:
            return
        self._dashboard_workspace.write_text(
            f"audit/{spec.id}/meta.json",
            json.dumps({"repo": spec.repo, "mode": spec.mode, "id": spec.id}),
        )

    def _mirror_verification(self, source_job: str, entry: Dict[str, Any]) -> None:
        """Append one verification verdict to the source job's history.

        Read-modify-write because :class:`~dev_team.execution.Workspace` has
        no append primitive; the single-flight worker guarantees no
        concurrent writers. Guarded for a missing dashboard workspace even
        though a verify job cannot be submitted without one — fail safe.
        """

        if self._dashboard_workspace is None:
            return
        path = f"audit/{source_job}/verifications.jsonl"
        existing = (
            self._dashboard_workspace.read_text(path)
            if self._dashboard_workspace.exists(path)
            else ""
        )
        self._dashboard_workspace.write_text(
            path, existing + json.dumps(entry) + "\n"
        )

    # -- cancel (job lifecycle) -----------------------------------------------

    def cancel_job(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /jobs/{id}/cancel`` core: pull a still-queued job out.

        Reachable only from ``queued`` — the one rung the documented state
        machine (``queued -> running -> succeeded | failed``) is otherwise
        missing: once a job is ``running`` it is out of scope (an in-flight
        clone/agent session, same boundary ``archive_job`` already draws).
        Non-idempotent by design (409 on every other state, including an
        already-cancelled job) — every other transition here is one-way, so
        mixing an idempotent cancel into this route table would be less
        consistent than a 409 on a redundant call. Shares ``self._lock``
        with :meth:`_execute`'s queued->running flip, so the two transitions
        are mutually exclusive: whichever call wins the race decides the
        outcome deterministically, and a job can never end up both running
        and marked cancelled.
        """

        with self._lock:
            record = self._registry.get(job_id)
            if record is None:
                return 404, {"error": "unknown job"}
            if record.state != "queued":
                return 409, {"error": "job is not queued", "state": record.state}
            record.state = "cancelled"
            record.ended = self._clock()
        self._events[job_id].set()
        return 200, {"id": job_id, "state": "cancelled"}

    # -- interactive question/answer (opt-in deliver pause) --------------------

    def get_question(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``GET /jobs/{id}/question`` core: peek the live pending question.

        Non-destructive — reads :attr:`_TrackedChannel.current` under
        ``self._lock`` (the same lock every other job-state read/write
        already uses) without touching :attr:`QueueChannel.questions`, which
        only the engine's own blocking ``ask()`` may ever drain. ``pending``
        is ``False`` for a job that was never interactive, one with no live
        pause right now, or one whose question was already answered.
        """

        with self._lock:
            record = self._registry.get(job_id)
            if record is None:
                return 404, {"error": "unknown job"}
            channel = record.channel
            question = channel.current if channel is not None else None
        if question is None:
            return 200, {"pending": False}
        return 200, {
            "pending": True,
            "prompt": question.prompt,
            "context": question.context,
            "choices": [
                {"key": c.key, "label": c.label, "accepts_text": c.accepts_text}
                for c in question.choices
            ],
            "default": question.default.key,
        }

    def answer_question(
        self, job_id: str, body: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /jobs/{id}/answer`` core: unblock a paused ``ask()``.

        ``choice`` is validated against — and delivered to — the LIVE
        question atomically inside :meth:`_TrackedChannel.submit_reply`,
        never trusted as free-form and never split across a
        validate-then-deliver gap: an invalid choice can never unblock the
        wrong (or any) answer, and a choice that *was* live when checked
        can never end up delivered to a different question the engine has
        since moved on to (e.g. after a timeout took the default). Touches
        nothing on ``record.spec``: this cannot be used to approve a
        push/deploy/rm, which stays gated by the unchanged
        ``PolicyApprovalGate`` wired in :meth:`run_job`.
        """

        with self._lock:
            record = self._registry.get(job_id)
            if record is None:
                return 404, {"error": "unknown job"}
            channel = record.channel
        if channel is None:
            return 409, {"error": "no pending question"}
        choice = body.get("choice")
        if not isinstance(choice, str):
            return 400, {"error": "unknown choice"}
        text = body.get("text", "")
        if not isinstance(text, str):
            text = ""
        result = channel.submit_reply(choice, text)
        if result is None:
            return 409, {"error": "no pending question"}
        if result is False:
            return 400, {"error": "unknown choice"}
        return 202, {}

    # -- archive / unarchive (job lifecycle) ----------------------------------

    def _meta_path(self, job_id: str) -> str:
        return f"audit/{job_id}/meta.json"

    def _read_meta(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Parsed ``meta.json`` for ``job_id``, or ``None`` if unavailable.

        Fails closed exactly like :meth:`_exists`: a traversal-shaped id, a
        job whose assessment was never mirrored, and a corrupt file are all
        indistinguishable from "no such job" to the caller — never a 500.
        """

        if self._dashboard_workspace is None:
            return None
        path = self._meta_path(job_id)
        if not self._exists(path):
            return None
        try:
            return json.loads(self._dashboard_workspace.read_text(path))
        except (OSError, ValueError, WorkspaceError):
            return None

    def _is_archived(self, job_id: str) -> bool:
        """Whether ``job_id``'s mirrored ``meta.json`` is marked archived."""

        meta = self._read_meta(job_id)
        return bool(meta and meta.get("archived", False))

    def _job_running(self, job_id: str) -> bool:
        """Whether ``job_id`` is ``queued`` or ``running`` in the registry."""

        with self._lock:
            record = self._registry.get(job_id)
            return record is not None and record.state not in _TERMINAL

    def _set_archived(self, job_id: str, archived: bool) -> Tuple[int, Dict[str, Any]]:
        """Shared core for archive/unarchive: flip the ``archived`` marker.

        Requires a persisted ``meta.json`` (the same precondition every
        other disk-keyed route enforces) — missing or corrupt is a 404, not
        a 500. Archiving (never unarchiving) a job still ``queued`` or
        ``running`` is refused with 409: its files are still being written
        by the single-flight worker. Unarchiving a job that is not archived
        is idempotent (200), matching every other idempotent mutation here.
        """

        if self._dashboard_workspace is None:
            return 409, {"error": "archive needs a dashboard workspace"}
        if archived and self._job_running(job_id):
            return 409, {"error": "job is running"}
        with self._meta_lock:
            meta = self._read_meta(job_id)
            if meta is None:
                return 404, {"error": "no assessment for that job"}
            if archived:
                meta["archived"] = True
                meta["archived_at"] = self._clock()
            else:
                meta.pop("archived", None)
                meta.pop("archived_at", None)
            self._dashboard_workspace.write_text(
                self._meta_path(job_id), json.dumps(meta)
            )
        return 200, {"id": job_id, "archived": archived}

    def archive_job(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /jobs/{id}/archive`` core."""

        return self._set_archived(job_id, True)

    def unarchive_job(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /jobs/{id}/unarchive`` core."""

        return self._set_archived(job_id, False)

    # -- purge (permanent deletion) -------------------------------------------

    def _purge_backlog_stories(self, job_id: str) -> int:
        """Remove every backlog story bred from ``job_id``, edges included.

        Shares ``self._backlog_lock`` with :meth:`delete_story` /
        :meth:`_mutate_backlog` (the ``DELETE /backlog/story/{id}`` core) so a
        concurrent board write can never interleave into a corrupt
        ``backlog.json``. A no-op (no save) when nothing matches, exactly
        like :meth:`make_backlog` on an empty merge.
        """

        store = BacklogStore(self._dashboard_workspace)
        with self._backlog_lock:
            backlog = store.load()
            removed_ids = {s.id for s in backlog.stories if s.source_job == job_id}
            if not removed_ids:
                return 0
            backlog.stories = [s for s in backlog.stories if s.id not in removed_ids]
            for survivor in backlog.stories:
                survivor.depends_on = [
                    dep for dep in survivor.depends_on if dep not in removed_ids
                ]
            store.save(backlog)
        return len(removed_ids)

    def purge_job(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /jobs/{id}/purge`` core: permanent, archive-gated deletion.

        Removes exactly three things: the workspace clone
        (``jobs_root/<id>``, ``shutil.rmtree`` — it already sits outside the
        :class:`Workspace` abstraction, the same raw ``Path`` join
        :meth:`run_job` itself uses), the ``audit/<id>/`` mirror (each of
        :data:`_AUDIT_FILES` through ``self._dashboard_workspace.delete`` —
        **never** a raw filesystem call against the dashboard workspace, so
        the existing traversal/symlink-escape guard on that abstraction
        (``_within_root``) still applies), and any backlog stories bred from
        this job (:meth:`_purge_backlog_stories`).

        The terminal-state check happens directly against ``record.state``
        inside a single ``self._lock`` block — never via :meth:`_job_running`,
        which itself acquires ``self._lock`` and would deadlock the caller
        (and, because every other mutation shares this lock, the whole
        single-flight dispatcher). The registry entry is deleted in that same
        block, so a purged job is gone for good: a second call finds no
        record and answers 404, never an idempotent 200.
        """

        with self._lock:
            record = self._registry.get(job_id)
            if record is None:
                return 404, {"error": "unknown job"}
            if record.state not in _TERMINAL:
                return 409, {"error": "job is running"}
            if not self._is_archived(job_id):
                return 409, {"error": "job is not archived"}
            del self._registry[job_id]
            self._order.remove(job_id)
            self._events.pop(job_id, None)

        workspace_dir = Path(self._jobs_root) / job_id
        removed_workspace = workspace_dir.exists()
        shutil.rmtree(workspace_dir, ignore_errors=True)

        removed_audit = False
        for name in _AUDIT_FILES:
            path = f"audit/{job_id}/{name}"
            try:
                if self._dashboard_workspace.exists(path):
                    removed_audit = True
                    self._dashboard_workspace.delete(path)
            except WorkspaceError:
                # A symlink planted at this path resolves outside the
                # dashboard workspace root — refused by the workspace's own
                # escape check, not silently followed. Leave it in place
                # rather than raising out of a purge that already succeeded
                # for the rest of the job's state.
                continue

        removed_stories = self._purge_backlog_stories(job_id)

        return 200, {
            "id": job_id,
            "purged": True,
            "removed": {
                "workspace": removed_workspace,
                "audit": removed_audit,
                "backlog_stories": removed_stories,
            },
        }

    def list_job_findings(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``GET /jobs/{id}/findings`` core: the re-checkable claims.

        Reads ``audit/<job_id>/assessment.json`` from the dashboard
        workspace — disk, never the in-memory registry, so it keeps working
        for jobs that ran before a service restart. Returns
        ``(status_code, payload)`` exactly as the HTTP layer sends it.
        """

        if self._dashboard_workspace is None:
            return 409, {"error": "findings need a dashboard workspace"}
        path = f"audit/{job_id}/assessment.json"
        if not self._exists(path):
            return 404, {"error": "no assessment for that job"}
        # A corrupt mirror is answered 404, not a 500 (see _verify_spec).
        try:
            data = json.loads(self._dashboard_workspace.read_text(path))
        except (OSError, ValueError, WorkspaceError):
            return 404, {"error": "no assessment for that job"}
        return 200, {"job_id": job_id, "findings": list_findings(data)}

    def verifications(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``GET /jobs/{id}/verifications`` core: verdicts, chronological.

        Disk-keyed like :meth:`list_job_findings`; the jsonl is append-order,
        which IS chronological under the single-flight worker.
        """

        if self._dashboard_workspace is None:
            return 409, {"error": "verifications need a dashboard workspace"}
        if not self._exists(f"audit/{job_id}/assessment.json"):
            return 404, {"error": "no assessment for that job"}
        path = f"audit/{job_id}/verifications.jsonl"
        entries: List[Dict[str, Any]] = []
        if self._exists(path):
            for line in self._dashboard_workspace.read_text(path).splitlines():
                if not line.strip():
                    continue
                # Skip a corrupt line (partial append, hand-edit) rather than
                # letting it 500 the whole endpoint — same forgiveness as
                # eventlog.read_events.
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        return 200, {"job_id": job_id, "verifications": entries}

    def calibration(self) -> Tuple[int, Dict[str, Any]]:
        """The ``GET /calibration`` core: verdict calibration, across every job.

        Walks every ``audit/*/verifications.jsonl`` in the dashboard
        workspace, tolerantly parsing each line (a corrupt line is skipped,
        same as :meth:`verifications`), and rolls the union up with
        :func:`calibration_summary`. A job whose ``meta.json`` is marked
        archived is skipped entirely — its verdicts must not skew the
        rollup — and reappears once unarchived. ``jobs_counted`` is the
        number of files that contributed at least one parseable line — a
        pure, $0, disk-only aggregate, like :meth:`make_backlog`.
        """

        if self._dashboard_workspace is None:
            return 409, {"error": "calibration needs a dashboard workspace"}
        entries: List[Dict[str, Any]] = []
        jobs_counted = 0
        for path in self._dashboard_workspace.list_files():
            if not path.startswith("audit/") or not path.endswith(
                "/verifications.jsonl"
            ):
                continue
            parts = path.split("/")
            if len(parts) == 3 and self._is_archived(parts[1]):
                continue
            contributed = False
            for line in self._dashboard_workspace.read_text(path).splitlines():
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
                contributed = True
            if contributed:
                jobs_counted += 1
        return 200, {**calibration_summary(entries), "jobs_counted": jobs_counted}

    def costs(self, *, include_archived: bool = False) -> Tuple[int, Dict[str, Any]]:
        """The ``GET /costs`` core: total spend rollup across every job.

        Source of truth is the in-memory registry, not disk: unlike
        verdicts, ``deliver`` job cost is never mirrored to disk, so a
        disk walk would silently under-report. Snapshots the registry
        under ``self._lock`` exactly like :meth:`recent`, then filters and
        sums outside it — archived-exclusion does a disk read via
        :meth:`_is_archived`, which must never run while the lock is held.

        Only ``succeeded``/``failed`` jobs have a non-``None`` ``cost_usd``
        (``queued``/``running`` never set it; ``cancel_job`` never touches
        it), matching :meth:`result`'s "finished" framing — those are the
        only ones counted. No dashboard-workspace guard is needed:
        :meth:`_is_archived` returns ``False`` (never raises) when
        ``self._dashboard_workspace is None``, so archived-exclusion is
        simply a no-op until one is configured.
        """

        with self._lock:
            records = list(self._registry.values())
        total_usd = 0.0
        by_mode: Dict[str, float] = {}
        jobs_counted = 0
        for record in records:
            if record.cost_usd is None:
                continue
            if not include_archived and self._is_archived(record.spec.id):
                continue
            total_usd += record.cost_usd
            by_mode[record.spec.mode] = by_mode.get(record.spec.mode, 0.0) + record.cost_usd
            jobs_counted += 1
        return 200, {
            "total_usd": total_usd,
            "by_mode": by_mode,
            "jobs_counted": jobs_counted,
        }

    def _merge_backlog(
        self,
        data: Dict[str, Any],
        *,
        repo: Optional[str] = None,
        source_job: Optional[str] = None,
    ) -> Tuple[int, int]:
        """Merge a serialised assessment into the dashboard-workspace backlog.

        ``repo``/``source_job`` flow into :func:`dict_to_backlog`: with a
        repo the stories land under that repository's own epic and carry
        finding provenance. Returns ``(stories_added, stories_total)``.
        Callers guarantee a dashboard workspace is configured. The whole
        read-modify-write runs under the shared backlog lock: this method is
        called from the worker thread (assess ``--backlog``) and from handler
        threads (``make_backlog``), which would otherwise clobber each other.
        """

        store = BacklogStore(self._dashboard_workspace)
        with self._backlog_lock:
            backlog = store.load()
            added = dict_to_backlog(data, backlog, repo=repo, source_job=source_job)
            store.save(backlog)
        return len(added), len(backlog.stories)

    def make_backlog(self, job_id: str) -> Tuple[int, Dict[str, Any]]:
        """Generate backlog stories from a finished assess job — later, free.

        The ``POST /jobs/{id}/backlog`` core: reads
        ``audit/<job_id>/assessment.json`` from the dashboard workspace (disk,
        never the in-memory registry, so it survives a service restart) and
        merges its findings into the workspace's ``.dev_team/backlog.json``.
        A pure transform — no agents, no LLM calls, $0. Returns
        ``(status_code, payload)`` exactly as the HTTP layer sends it.
        """

        if self._dashboard_workspace is None:
            return 409, {"error": "backlog generation needs a dashboard workspace"}
        path = f"audit/{job_id}/assessment.json"
        # Route through _exists (fails closed) like the sibling readers so a
        # traversal-shaped job id answers 404 instead of raising out of the
        # workspace's path guard (a 500).
        if not self._exists(path):
            return 404, {"error": "no assessment for that job"}
        # A corrupt mirror is answered 404, not a 500 (see _verify_spec).
        try:
            data = json.loads(self._dashboard_workspace.read_text(path))
        except (OSError, ValueError, WorkspaceError):
            return 404, {"error": "no assessment for that job"}
        # meta.json (mirrored beside every assessment since verify landed)
        # names the audited repo — that keys the per-repository epic. Older
        # mirrors without it (or a corrupt one) fall back to the single shared
        # epic rather than failing the whole request.
        repo = None
        meta_path = f"audit/{job_id}/meta.json"
        if self._exists(meta_path):
            try:
                meta = json.loads(self._dashboard_workspace.read_text(meta_path))
            except (OSError, ValueError, WorkspaceError):
                meta = {}
            repo = meta.get("repo")
        added, total = self._merge_backlog(data, repo=repo, source_job=job_id)
        return 200, {
            "job_id": job_id,
            "stories_added": added,
            "stories_total": total,
        }

    # -- backlog mutation API (the Kanban board's write path) -----------------
    # Each core is the (status, payload) behind one /backlog route: load the
    # dashboard workspace's backlog, mutate, stamp updated_at, save — the
    # whole read-modify-write under the shared backlog lock, so handler
    # threads and the worker's _merge_backlog never lose each other's writes.
    # Synchronous like make_backlog: pure disk transforms, no queue slot.

    def _mutate_backlog(
        self, mutate: Callable[[Backlog], Tuple[int, Dict[str, Any]]]
    ) -> Tuple[int, Dict[str, Any]]:
        """Run one locked load→mutate→save; persist only successful mutations."""

        if self._dashboard_workspace is None:
            return 409, {"error": "backlog needs a dashboard workspace"}
        store = BacklogStore(self._dashboard_workspace)
        with self._backlog_lock:
            backlog = store.load()
            status, payload = mutate(backlog)
            if status < 400:
                store.save(backlog)
        return status, payload

    def _mutate_story(
        self,
        story_id: str,
        apply: Callable[[Backlog, Story], Optional[Tuple[int, Dict[str, Any]]]],
    ) -> Tuple[int, Dict[str, Any]]:
        """Mutate one story by id: 404 unknown ids, stamp + echo on success.

        ``apply`` returns ``None`` on success or its own ``(status, payload)``
        rejection (nothing is persisted for a rejection).
        """

        def mutate(backlog: Backlog) -> Tuple[int, Dict[str, Any]]:
            story = next((s for s in backlog.stories if s.id == story_id), None)
            if story is None:
                return 404, {"error": "unknown story"}
            rejected = apply(backlog, story)
            if rejected is not None:
                return rejected
            story.updated_at = self._clock()
            return 200, _story_to_dict(story)

        return self._mutate_backlog(mutate)

    def set_story_status(
        self, story_id: str, status: Any
    ) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /backlog/story/{id}/status`` core."""

        values = [item.value for item in ItemStatus]
        if status not in values:
            return 400, {"error": f"status must be one of: {', '.join(values)}"}

        def apply(backlog: Backlog, story: Story) -> None:
            story.status = ItemStatus(status)

        return self._mutate_story(story_id, apply)

    def decline_story(self, story_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /backlog/story/{id}/decline`` core: status → declined."""

        return self.set_story_status(story_id, ItemStatus.DECLINED.value)

    def edit_story(
        self, story_id: str, fields: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        """The ``PATCH /backlog/story/{id}`` core: apply only provided keys."""

        title = fields.get("title")
        if "title" in fields and (not isinstance(title, str) or not title.strip()):
            return 400, {"error": "title must be a non-empty string"}
        if "description" in fields and not isinstance(fields["description"], str):
            return 400, {"error": "description must be a string"}
        estimate = fields.get("estimate")
        if "estimate" in fields and (
            isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 1
        ):
            return 400, {"error": "estimate must be an integer of at least 1"}

        def apply(backlog: Backlog, story: Story) -> None:
            if "title" in fields:
                story.title = title
            if "description" in fields:
                story.description = fields["description"]
            if "estimate" in fields:
                story.estimate = estimate

        return self._mutate_story(story_id, apply)

    def add_story_card(self, fields: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /backlog/story`` core: a hand-written story card."""

        title = fields.get("title")
        if not isinstance(title, str) or not title.strip():
            return 400, {"error": "title is required"}
        description = fields.get("description", "")
        if not isinstance(description, str):
            return 400, {"error": "description must be a string"}
        estimate = fields.get("estimate", 1)
        if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 1:
            return 400, {"error": "estimate must be an integer of at least 1"}
        epic_id = fields.get("epic_id")
        if epic_id is not None and not isinstance(epic_id, str):
            return 400, {"error": "epic_id must be a string"}
        status = fields.get("status", ItemStatus.TODO.value)
        values = [item.value for item in ItemStatus]
        if status not in values:
            return 400, {"error": f"status must be one of: {', '.join(values)}"}

        def mutate(backlog: Backlog) -> Tuple[int, Dict[str, Any]]:
            story = backlog.add_story(
                title, description, estimate=estimate, epic_id=epic_id
            )
            story.status = ItemStatus(status)
            story.updated_at = self._clock()
            return 201, _story_to_dict(story)

        return self._mutate_backlog(mutate)

    def delete_story(self, story_id: str) -> Tuple[int, Dict[str, Any]]:
        """The ``DELETE /backlog/story/{id}`` core: remove + strip its edges.

        Inbound ``depends_on`` edges are stripped from every surviving story
        so the file never holds a dangling reference — and because ids are
        minted past the highest suffix ever used, the deleted id can never be
        reissued to an unrelated future story.
        """

        def mutate(backlog: Backlog) -> Tuple[int, Dict[str, Any]]:
            story = next((s for s in backlog.stories if s.id == story_id), None)
            if story is None:
                return 404, {"error": "unknown story"}
            backlog.stories.remove(story)
            for survivor in backlog.stories:
                survivor.depends_on = [
                    dep for dep in survivor.depends_on if dep != story_id
                ]
            story.updated_at = self._clock()
            return 200, _story_to_dict(story)

        return self._mutate_backlog(mutate)

    def set_story_deps(
        self, story_id: str, depends_on: Any
    ) -> Tuple[int, Dict[str, Any]]:
        """The ``POST /backlog/story/{id}/deps`` core: set validated edges."""

        if not isinstance(depends_on, list) or not all(
            isinstance(dep, str) for dep in depends_on
        ):
            return 400, {"error": "depends_on must be a list of story ids"}

        def apply(
            backlog: Backlog, story: Story
        ) -> Optional[Tuple[int, Dict[str, Any]]]:
            story.depends_on = list(depends_on)
            try:
                validate_dependencies(backlog)
            except (DependencyCycleError, ValueError) as exc:
                # Rejected mutations are never saved, so the in-memory edit
                # is discarded with the loaded backlog.
                return 400, {"error": str(exc)}
            return None

        return self._mutate_story(story_id, apply)

    def board(self) -> Tuple[int, Dict[str, Any]]:
        """The ``GET /backlog`` core: the full serialised backlog."""

        if self._dashboard_workspace is None:
            return 409, {"error": "backlog needs a dashboard workspace"}
        with self._backlog_lock:
            backlog = BacklogStore(self._dashboard_workspace).load()
        return 200, backlog.to_dict()

    # -- serialisation -------------------------------------------------------

    def _progress(self, record: JobRecord) -> List[Dict[str, Any]]:
        if record.workspace is None:
            return []
        events = read_events(record.workspace)
        return [
            {
                "role": event.get("role"),
                "stage": event.get("stage"),
                "message": event.get("message"),
                "ts": event.get("ts"),
            }
            for event in events[-_PROGRESS_LIMIT:]
        ]

    def status(self, record: JobRecord) -> Dict[str, Any]:
        """The ``GET /jobs/{id}`` payload for ``record``."""

        return {
            "id": record.spec.id,
            "mode": record.spec.mode,
            "repo": record.spec.repo,
            "state": record.state,
            "started": record.started,
            "ended": record.ended,
            "cost_usd": record.cost_usd,
            "error": record.error,
            "progress": self._progress(record),
        }

    def summary(self, record: JobRecord) -> Dict[str, Any]:
        """One entry in the ``GET /jobs`` list."""

        return {
            "id": record.spec.id,
            "mode": record.spec.mode,
            "repo": record.spec.repo,
            "state": record.state,
            "started": record.started,
            "ended": record.ended,
        }

    def result(self, record: JobRecord) -> Tuple[int, Dict[str, Any]]:
        """The ``GET /jobs/{id}/result`` ``(status_code, payload)``."""

        if record.state not in _TERMINAL:
            return 409, {"error": "not finished", "state": record.state}
        if record.state == "failed":
            # Serve the real (possibly partial) spend banked before the
            # failure, not a literal 0 — a job that burned budget and then
            # raised must not report $0 (see _failed_cost / run_job).
            return 200, {
                "kind": record.spec.mode,
                "success": False,
                "error": record.error,
                "cost_usd": record.cost_usd,
            }
        if record.state == "cancelled":
            # A cancelled job never ran, so record.outcome stays None —
            # must not fall through to the outcome-dereferencing branches
            # below.
            return 200, {
                "kind": record.spec.mode,
                "success": False,
                "error": "cancelled",
                "cost_usd": 0,
            }
        outcome = record.outcome
        if record.spec.mode == "verify":
            payload = {
                "kind": "verify",
                "source_job": record.spec.source_job,
                "finding_id": outcome["finding_id"],
                "verdict": outcome["verdict"],
                "rationale": outcome["rationale"],
                "citations": outcome["citations"],
                "cost_usd": outcome["cost_usd"],
            }
            if outcome.get("skipped"):
                # Distinguishes a $0 deterministic skip from a real agent
                # verdict — see verify_finding's skip_broken_citations.
                payload["success"] = True
                payload["skipped"] = True
            return 200, payload
        if record.spec.mode == "assess":
            return 200, {
                "kind": "assess",
                "success": outcome.success,
                "classification": outcome.classification,
                "executive_summary": outcome.executive_summary,
                "report_path": outcome.report_path,
                "report_markdown": outcome.report_markdown,
                "cost_usd": outcome.cost_usd,
            }
        return 200, {"kind": "deliver", **delivery_to_dict(outcome)}


def _make_handler(dispatcher: Dispatcher) -> type:
    """A request handler class bound to ``dispatcher``."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            """Silence per-request stderr noise; the CLI prints the URL once."""

        def handle_one_request(self) -> None:  # noqa: N802 (http.server API)
            """Append one access-log record after every request, unconditionally.

            Every ``do_GET``/``do_POST``/``do_PATCH``/``do_DELETE`` branch —
            and the stdlib's own pre-dispatch error handling (a malformed
            request line, an unsupported method) — funnels its response
            through :meth:`send_response`, so overriding it below to record
            the status actually sent, then reading that back here in a
            ``finally``, captures every request's outcome regardless of which
            branch (if any) handled it. Nothing is logged if the connection
            dropped before any response was ever sent.

            The log write is best-effort: an ``OSError`` (disk full,
            unwritable jobs root) is swallowed here so it can never turn an
            otherwise-successful response already sent to the caller into a
            handler crash.
            """

            self._access_log_status: Optional[int] = None
            try:
                super().handle_one_request()
            finally:
                status = self._access_log_status
                if status is not None:
                    try:
                        dispatcher.access_log.append(
                            method=getattr(self, "command", None) or "-",
                            request_path=urlsplit(getattr(self, "path", "") or "").path,
                            status=status,
                        )
                    except OSError:
                        pass

        def send_response(self, code: int, message: Optional[str] = None) -> None:
            self._access_log_status = code
            super().send_response(code, message)

        def _json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _int_param(
            self, query: Dict[str, List[str]], name: str, default: int
        ) -> int:
            """A non-negative-ish int query param, or ``default`` when absent/junk.

            Query values are attacker-controlled, so a missing or non-numeric
            ``?limit=``/``?offset=`` falls back to the default rather than
            erroring; :meth:`Dispatcher.recent` clamps the value to a sane
            range. Mirrors the lenient exact-match handling of ``?archived=``.
            """

            values = query.get(name)
            if not values:
                return default
            try:
                return int(values[0])
            except ValueError:
                return default

        def _authorised(self) -> bool:
            """Constant-time bearer check over the whole header value.

            Compares UTF-8 *bytes*, not str: headers decode as latin-1, so a
            non-ASCII ``Authorization`` value is a valid str that
            :func:`hmac.compare_digest` refuses (it raises on non-ASCII text).
            The header is attacker-controlled and reached before auth, so a
            stray byte must read as "no match", never crash the handler
            (which would be a pre-auth 500/connection-reset DoS). Mirrors the
            dashboard's ``_tokens_match``.
            """

            expected = f"Bearer {dispatcher.token}"
            provided = self.headers.get("Authorization", "")
            return hmac.compare_digest(
                provided.encode("utf-8"), expected.encode("utf-8")
            )

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            split = urlsplit(self.path)
            path = split.path
            if path == "/health":
                from . import __version__

                self._json(
                    200,
                    {
                        "status": "ok",
                        "service": "dev-team-dispatch",
                        "version": __version__,
                    },
                )
                return
            if not self._authorised():
                self._json(401, {"error": "unauthorized"})
                return
            if path == "/jobs":
                # ?archived=1 reveals archived jobs too; any other value (or
                # its absence) keeps the default exclusion. ?limit=/?offset=
                # page the newest-first list (bounds enforced by recent()).
                query = parse_qs(split.query)
                include_archived = query.get("archived", ["0"])[0] == "1"
                limit = self._int_param(query, "limit", _LIST_LIMIT)
                offset = self._int_param(query, "offset", 0)
                self._json(
                    200,
                    {
                        "jobs": [
                            dispatcher.summary(r)
                            for r in dispatcher.recent(
                                limit=limit,
                                offset=offset,
                                include_archived=include_archived,
                            )
                        ]
                    },
                )
                return
            if path == "/backlog":
                status, payload = dispatcher.board()
                self._json(status, payload)
                return
            if path == "/calibration":
                status, payload = dispatcher.calibration()
                self._json(status, payload)
                return
            if path == "/costs":
                # ?archived=1 reveals archived jobs too — same exact-match
                # contract as /jobs.
                include_archived = (
                    parse_qs(split.query).get("archived", ["0"])[0] == "1"
                )
                status, payload = dispatcher.costs(include_archived=include_archived)
                self._json(status, payload)
                return
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "jobs":
                self._job(parts[1])
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "result":
                self._result(parts[1])
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "findings":
                # Disk-keyed (no registry lookup): works after a restart.
                status, payload = dispatcher.list_job_findings(parts[1])
                self._json(status, payload)
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "verifications":
                status, payload = dispatcher.verifications(parts[1])
                self._json(status, payload)
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "question":
                status, payload = dispatcher.get_question(parts[1])
                self._json(status, payload)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            path = urlsplit(self.path).path
            if not self._authorised():
                self._json(401, {"error": "unauthorized"})
                return
            if path == "/jobs":
                self._create()
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "backlog":
                # Synchronous on purpose: a pure disk transform (no agents,
                # no queue slot needed) that answers in one round-trip.
                status, payload = dispatcher.make_backlog(parts[1])
                self._json(status, payload)
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                # Same shape as .../backlog above: a pure in-memory
                # transition, synchronous, no queue slot.
                status, payload = dispatcher.cancel_job(parts[1])
                self._json(status, payload)
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "answer":
                body = self._read_body()
                if body is None:
                    return
                status, payload = dispatcher.answer_question(parts[1], body)
                self._json(status, payload)
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] in (
                "archive",
                "unarchive",
            ):
                # Same shape as .../backlog above: a pure disk transform,
                # synchronous, no queue slot.
                if parts[2] == "archive":
                    status, payload = dispatcher.archive_job(parts[1])
                else:
                    status, payload = dispatcher.unarchive_job(parts[1])
                self._json(status, payload)
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "purge":
                # Same shape as .../archive above: a pure disk transform,
                # synchronous, no queue slot.
                status, payload = dispatcher.purge_job(parts[1])
                self._json(status, payload)
                return
            if parts[0] == "backlog":
                self._backlog_post(parts)
                return
            self._json(404, {"error": "not found"})

        def do_PATCH(self) -> None:  # noqa: N802 (http.server API)
            path = urlsplit(self.path).path
            if not self._authorised():
                self._json(401, {"error": "unauthorized"})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "backlog" and parts[1] == "story":
                body = self._read_body()
                if body is None:
                    return
                status, payload = dispatcher.edit_story(parts[2], body)
                self._json(status, payload)
                return
            self._json(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802 (http.server API)
            path = urlsplit(self.path).path
            if not self._authorised():
                self._json(401, {"error": "unauthorized"})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "backlog" and parts[1] == "story":
                status, payload = dispatcher.delete_story(parts[2])
                self._json(status, payload)
                return
            self._json(404, {"error": "not found"})

        def _backlog_post(self, parts: List[str]) -> None:
            """Route an authorised ``POST /backlog/...`` to its core.

            Synchronous like ``make_backlog``: each core is a locked disk
            transform, so it answers in one round-trip without a queue slot.
            """

            if len(parts) == 2 and parts[1] == "story":
                body = self._read_body()
                if body is None:
                    return
                status, payload = dispatcher.add_story_card(body)
                self._json(status, payload)
                return
            if len(parts) == 4 and parts[1] == "story":
                story_id, action = parts[2], parts[3]
                if action == "status":
                    body = self._read_body()
                    if body is None:
                        return
                    status, payload = dispatcher.set_story_status(
                        story_id, body.get("status")
                    )
                    self._json(status, payload)
                    return
                if action == "decline":
                    status, payload = dispatcher.decline_story(story_id)
                    self._json(status, payload)
                    return
                if action == "deps":
                    body = self._read_body()
                    if body is None:
                        return
                    status, payload = dispatcher.set_story_deps(
                        story_id, body.get("depends_on")
                    )
                    self._json(status, payload)
                    return
            self._json(404, {"error": "not found"})

        def _read_body(self) -> Optional[Dict[str, Any]]:
            """The JSON object body, or ``None`` after answering the 400.

            Content-Length is attacker-controlled: a non-numeric value must
            not crash int() (a 500), and an oversized/negative one must not
            have us buffer an unbounded body. Mirror the dashboard's _login
            guard. A missing/zero length stays valid — it means "empty body".
            """

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._json(400, {"error": "malformed Content-Length"})
                return None
            if length > _MAX_BODY:
                self._json(413, {"error": "request body too large"})
                return None
            if length < 0:
                self._json(400, {"error": "malformed Content-Length"})
                return None
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "malformed JSON body"})
                return None
            if not isinstance(body, dict):
                self._json(400, {"error": "JSON body must be an object"})
                return None
            return body

        def _create(self) -> None:
            body = self._read_body()
            if body is None:
                return
            try:
                spec = dispatcher.build_spec(body)
            except ValidationError as exc:
                self._json(400, {"error": str(exc)})
                return
            except SubmitRejected as exc:
                self._json(exc.status, {"error": str(exc)})
                return
            try:
                job_id, position = dispatcher.submit(spec)
            except QueueFull:
                self._json(503, {"error": "queue full"})
                return
            self._json(202, {"id": job_id, "state": "queued", "position": position})

        def _job(self, job_id: str) -> None:
            record = dispatcher.get(job_id)
            if record is None:
                self._json(404, {"error": "unknown job"})
                return
            self._json(200, dispatcher.status(record))

        def _result(self, job_id: str) -> None:
            record = dispatcher.get(job_id)
            if record is None:
                self._json(404, {"error": "unknown job"})
                return
            status, payload = dispatcher.result(record)
            self._json(status, payload)

    return Handler


class DispatchServer:
    """The dispatch HTTP server wrapping a :class:`Dispatcher`.

    Mirrors :class:`~dev_team.dashboard.DashboardServer`: construct, then
    :meth:`serve_forever` (blocking) until :meth:`shutdown`. The worker thread
    starts on construction and stops on :meth:`shutdown`.
    """

    def __init__(
        self,
        token: str,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        runner: Optional[AgentRunner] = None,
        materialise: Optional[Callable[[JobSpec, str], Workspace]] = None,
        clock: Callable[[], float] = time.time,
        jobs_root: str = DEFAULT_JOBS_ROOT,
        queue_cap: int = DEFAULT_QUEUE_CAP,
        dashboard_workspace: Optional[Workspace] = None,
        record_transcripts: bool = False,
        job_timeout: float = _JOB_TIMEOUT_SECONDS,
    ) -> None:
        self.dispatcher = Dispatcher(
            token=token,
            runner=runner,
            materialise=materialise,
            clock=clock,
            jobs_root=jobs_root,
            queue_cap=queue_cap,
            dashboard_workspace=dashboard_workspace,
            record_transcripts=record_transcripts,
            job_timeout=job_timeout,
        )
        self.httpd = ThreadingHTTPServer((host, port), _make_handler(self.dispatcher))
        self.dispatcher.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dispatcher.stop()
