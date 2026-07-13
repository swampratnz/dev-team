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
class bound to a core object, and per-request stderr silenced. Three
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
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .assessment import (
    AssessConfig,
    dict_to_backlog,
    find_finding,
    list_findings,
    outcome_to_dict,
    verify_finding,
)
from .backlog import BacklogStore
from .budget import Budget
from .config import TeamConfig
from .engine import EngineConfig
from .errors import DevTeamError
from .eventlog import EventLog, compose, read_events
from .execution import (
    LocalWorkspace,
    SubprocessCommandRunner,
    Workspace,
    WorkspaceError,
)
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

#: How many of the newest jobs ``GET /jobs`` lists, and how many progress
#: events ``GET /jobs/{id}`` carries.
_LIST_LIMIT = 25
_PROGRESS_LIMIT = 12

#: The run modes the service accepts. ``verify`` re-checks one finding from
#: a previously mirrored assessment against a fresh clone of its repository.
_MODES = ("assess", "deliver", "verify")

#: Terminal job states.
_TERMINAL = frozenset({"succeeded", "failed"})

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
    id: str = ""
    # verify only: the source assess job, the resolved finding id, and the
    # RESOLVED finding itself (resolved synchronously at submit time so the
    # job cannot start and then discover the finding never existed).
    source_job: Optional[str] = None
    finding_id: Optional[str] = None
    finding: Optional[Dict[str, Any]] = None


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
    ) -> None:
        self.token = token
        self._runner = runner
        self._materialise = materialise or _default_materialise
        self._clock = clock
        self._jobs_root = jobs_root
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
        """Own an asyncio loop and run queued jobs strictly one at a time."""

        loop = asyncio.new_event_loop()
        try:
            while True:
                item = self._queue.get()
                if item is _SHUTDOWN:
                    return
                loop.run_until_complete(self._execute(item))
        finally:
            loop.close()

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
        data = json.loads(self._dashboard_workspace.read_text(assessment_path))
        finding = find_finding(data, finding_id.strip())
        if finding is None:
            raise SubmitRejected(404, "finding not found")
        meta = json.loads(self._dashboard_workspace.read_text(meta_path))
        return JobSpec(
            mode="verify",
            repo=str(meta.get("repo", "")),
            title=f"verify {finding['id']}",
            description="",
            budget_usd=budget,
            source_job=source_job,
            finding_id=finding["id"],
            finding=finding,
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

    def recent(self) -> List[JobRecord]:
        """The newest-first records, capped at :data:`_LIST_LIMIT`."""

        with self._lock:
            ordered = [self._registry[j] for j in self._order]
        return list(reversed(ordered))[:_LIST_LIMIT]

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
            record.state = "running"
            record.started = self._clock()
        try:
            outcome, cost = await self.run_job(record)
        except Exception as exc:  # noqa: BLE001 — a failed job must not kill the worker
            with self._lock:
                record.state = "failed"
                record.error = str(exc)
                record.cost_usd = 0.0
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
            return await self._run_verify(spec, workspace)
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
        team = DevTeam(
            self._runner,
            config=TeamConfig(),
            listener=listener,
            interaction=None,
        )
        budget = Budget(limit_usd=spec.budget_usd)
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
                # of flooding.
                self._merge_backlog(outcome_to_dict(outcome))
        else:
            outcome = await team.deliver(
                FeatureRequest(title=spec.title, description=spec.description),
                workspace=workspace,
                budget=budget,
                config=EngineConfig(commit=True),
                **kwargs,
            )
        return outcome, outcome.cost_usd

    async def _run_verify(self, spec: JobSpec, workspace: Workspace) -> Tuple[Any, float]:
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
        result = await verify_finding(
            runner,
            workspace,
            spec.finding,
            budget=Budget(limit_usd=spec.budget_usd),
            source_job=spec.source_job,
        )
        if not result["success"]:
            raise DevTeamError(str(result["error"]))
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
        data = json.loads(self._dashboard_workspace.read_text(path))
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
                if line.strip():
                    entries.append(json.loads(line))
        return 200, {"job_id": job_id, "verifications": entries}

    def _merge_backlog(self, data: Dict[str, Any]) -> Tuple[int, int]:
        """Merge a serialised assessment into the dashboard-workspace backlog.

        Returns ``(stories_added, stories_total)``. Callers guarantee a
        dashboard workspace is configured.
        """

        store = BacklogStore(self._dashboard_workspace)
        backlog = store.load()
        added = dict_to_backlog(data, backlog)
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
        if not self._dashboard_workspace.exists(path):
            return 404, {"error": "no assessment for that job"}
        data = json.loads(self._dashboard_workspace.read_text(path))
        added, total = self._merge_backlog(data)
        return 200, {
            "job_id": job_id,
            "stories_added": added,
            "stories_total": total,
        }

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
            return 200, {
                "kind": record.spec.mode,
                "success": False,
                "error": record.error,
                "cost_usd": 0,
            }
        outcome = record.outcome
        if record.spec.mode == "verify":
            return 200, {
                "kind": "verify",
                "source_job": record.spec.source_job,
                "finding_id": outcome["finding_id"],
                "verdict": outcome["verdict"],
                "rationale": outcome["rationale"],
                "citations": outcome["citations"],
                "cost_usd": outcome["cost_usd"],
            }
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

        def _json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            """Constant-time bearer check over the whole header value."""

            expected = f"Bearer {dispatcher.token}"
            provided = self.headers.get("Authorization", "")
            return hmac.compare_digest(provided, expected)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            path = urlsplit(self.path).path
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
                self._json(
                    200,
                    {"jobs": [dispatcher.summary(r) for r in dispatcher.recent()]},
                )
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
            self._json(404, {"error": "not found"})

        def _create(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "malformed JSON body"})
                return
            if not isinstance(body, dict):
                self._json(400, {"error": "JSON body must be an object"})
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
