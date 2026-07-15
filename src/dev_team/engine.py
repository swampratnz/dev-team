"""The delivery engine: real, gated, observable feature delivery.

Where :class:`~dev_team.workflow.DevelopmentWorkflow` *simulates* a run, the
:class:`DeliveryEngine` actually does the work. Its core guarantees:

- **Everything runs where the code lives.** Gates, git, and the agentic
  engineer all operate in the workspace root — never in the orchestrator's own
  working directory.
- **Agents see evidence, not summaries.** The reviewer, security engineer, and
  QA are shown the actual content of changed files; pass/fail comes from real
  exit codes.
- **Integration is serialised, implementation is parallel.** Engineers think
  concurrently, but apply → review → test → accept happens under a lock (a
  merge queue), and a failed attempt is rolled back so the workspace only ever
  accumulates work that passed its gates.
- **Accepted work is banked immediately.** Each task that passes its gates is
  committed as a ``wip(dev-team)`` commit on the delivery branch, so a later
  task's rollback (a hard reset) can never destroy it, and a crashed or
  over-budget run leaves committed work a resume can build on.
- **The feature commit waits for security.** The WIP commits collapse (soft
  reset to the delivery baseline) into a single curated feature commit at the
  end, and only when the security review did not block.
- **A blown budget stops the run, not the world.** Budget exhaustion fails
  remaining work gracefully and still returns a full (partial) outcome with
  trace, cost, and checkpoint intact — a later run resumes from the checkpoint.
"""

from __future__ import annotations

import asyncio
import fnmatch
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .agents import (
    ArchitectAgent,
    DevOpsAgent,
    EngineerAgent,
    ProductManagerAgent,
    QAAgent,
    ReviewerAgent,
    SecurityEngineerAgent,
    SREAgent,
    TechnicalWriterAgent,
)
from .agents.techwriter import doc_claim_issues
from .approval import ApprovalGate, ApprovalRequest, AutoApprover
from .backlog import BacklogStore, ItemStatus
from .budget import Budget, BudgetExceededError
from .changes import ChangeApplier
from .context import build_repo_context
from .conventions import ConventionsStore
from .agents.engineer import TOOLS as ENGINEER_TOOLS
from .errors import AgentResponseError, DependencyCycleError, DevTeamError
from .events import AgentEvent, Listener, emit
from .failures import new_failures, parse_failed_tests
from .execution import (
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    CommandRunner,
    DryRunCommandRunner,
    InMemoryWorkspace,
    LocalWorkspace,
    SubprocessCommandRunner,
    Workspace,
)
from .git import GitError, GitRepo
from .instrument import InstrumentedRunner, InstrumentedSession
from .interaction import (
    InteractionChannel,
    ask_in_thread,
    plan_review_question,
    replan_review_question,
    task_failure_question,
)
from .memory import (
    Blackboard,
    CheckpointStore,
    ProjectMemory,
    RunCheckpoint,
    task_fingerprint,
)
from .models import (
    ChangeType,
    Design,
    DeploymentPlan,
    Documentation,
    FeatureRequest,
    FileChange,
    Implementation,
    Plan,
    ReliabilityReport,
    Review,
    ReviewComment,
    SecurityReport,
    Severity,
    Task,
    TaskResult,
    TaskStatus,
    TestReport,
)
from .ordering import lint_plan
from .persona import Roster
from .policy import GuardedCommandRunner, SideEffectPolicy
from .profile import detect_project
from .replan import Replan, ReplanError, apply_replan
from .retrieval import char_budget_for_tokens, estimate_tokens, retrieve
from .sandbox import ContainerCommandRunner, SandboxConfig
from .scheduler import ScheduledResult, schedule
from .sdk import AgentRunner, AgentSession, ClaudeAgentSession
from .trace import Tracer
from .transcripts import TranscriptRecorder
from .verification import (
    CommandGate,
    DefinitionOfDone,
    DoDReport,
    GateContext,
    PredicateGate,
    RemoteCIGate,
)

# Internal bookkeeping lives under this prefix and is not part of the product.
_INTERNAL_PREFIX = ".dev_team/"


@dataclass
class EngineConfig:
    """Settings for a :class:`DeliveryEngine`.

    Notable fields:

    - ``verify_command``: the quality-gate command. ``None`` (the default)
      auto-detects it from the workspace's manifests (see
      :func:`~dev_team.profile.detect_project`).
    - ``setup_command``: run once in the workspace before anything else
      (e.g. ``("npm", "install")``); a failure halts the run cleanly.
    - ``require_green_baseline``: refuse to start on a workspace whose gates
      are already red — inherited breakage otherwise poisons every task and
      gets blamed on the engineer.
    - ``branch`` / ``use_branch``: agentic deliveries work on a dedicated
      ``dev-team/<feature>`` branch (never the caller's current branch).
    - ``allow_dirty_baseline``: by default a dirty working tree halts the run
      instead of being silently swept into a baseline commit.
    - ``engineer_tools``: override the agentic engineer's tool allowlist.
    - ``worktrees``: give each task its own git worktree so implementation
      *and* gate runs proceed in parallel; tasks are squash-merged into the
      delivery branch one at a time (with a full gate check on the merged
      state), and the accumulated WIP commits collapse into one feature
      commit after security approval. Requires agentic mode.
    - ``tolerate_baseline_failures``: when a red baseline is tolerated
      (``require_green_baseline=False``), record the failing test identities
      and gate tasks only on *newly* failing tests.
    - ``lint_command``: static analysis run over each attempt; its output is
      handed to the reviewer for triage (grounding review in tool findings).
    - ``security_scan_command``: SAST/dependency scanner whose output the
      security agent triages; defaults to the detected project profile's
      suggestion (bandit / npm audit).
    - ``fail_to_pass_check``: after gates pass, re-run them with the
      implementation reverted; if the tests still pass they never exercised
      the change, and the attempt is rejected as vacuous (SWT-bench logic).
      Skipped for dry runs, and whenever verification is remote or degraded
      (re-running those "gates" on reverted code proves nothing).
    - ``remote_verify_status`` / ``remote_verify_trigger``: delegate
      verification to an external CI system (see
      :class:`~dev_team.verification.RemoteCIGate`) — the escape hatch for
      repositories whose build only runs remotely (e.g. legacy .NET
      Framework on a Windows pipeline). ``status_command`` is polled until
      it exits zero; the optional trigger kicks the remote run off first.
    """

    model: Optional[str] = None
    max_task_attempts: int = 3
    max_concurrency: int = 4
    verify_command: Optional[Sequence[str]] = None
    setup_command: Optional[Sequence[str]] = None
    commit: bool = True
    agentic: Optional[bool] = None
    qa_tests: bool = True
    json_retries: int = 1
    role_models: Mapping[str, str] = field(default_factory=dict)
    escalation_model: Optional[str] = None
    resume: bool = True
    require_green_baseline: bool = True
    tolerate_baseline_failures: bool = True
    branch: Optional[str] = None
    use_branch: bool = True
    allow_dirty_baseline: bool = False
    write_gitignore: bool = True
    gate_timeout_seconds: Optional[float] = 1800.0
    engineer_tools: Optional[Sequence[str]] = None
    #: Reuse one persistent SDK session across a task's engineer attempts, so a
    #: retry continues the prior conversation (the code it read, the changes it
    #: made) rather than restarting cold — the big token saving on the shared
    #: pool. Off by default (opt-in); agentic, non-worktree only. A session turn
    #: that errors falls back to a cold attempt, and per-attempt model
    #: escalation does not apply on the session path (the session's model is
    #: fixed for its life). See ROADMAP #5.
    reuse_engineer_session: bool = False
    #: Retrieve the workspace's most-relevant code into the architect's prompt
    #: (and, later, the described engineer's), instead of only the repo map's
    #: path tree. Off by default (opt-in); deterministic lexical ranking, see
    #: ``dev_team.retrieval`` / ROADMAP #4.
    retrieval: bool = False
    #: Per-role budget, in estimated tokens, for retrieved code in a prompt.
    retrieval_token_budget: int = 3000
    worktrees: bool = False
    lint_command: Optional[Sequence[str]] = None
    security_scan_command: Optional[Sequence[str]] = None
    fail_to_pass_check: bool = True
    remote_verify_status: Optional[Sequence[str]] = None
    remote_verify_trigger: Optional[Sequence[str]] = None
    remote_verify_max_polls: int = 30
    remote_verify_interval_seconds: float = 60.0
    #: How many rounds of dynamic re-planning to run after the schedule leaves
    #: tasks failed. 0 (the default) keeps the current behaviour — a failed task
    #: just stays failed. When >0, the product manager proposes a mutation
    #: (split/replace/drop) for each still-failed task and the not-yet-attempted
    #: tasks of the mutated plan are re-scheduled, bounded by this count and the
    #: budget. A human supervises each mutation when an interaction channel is
    #: attached; otherwise it applies autonomously. See ``docs/ROADMAP.md`` #3.
    max_replan_rounds: int = 0
    #: When set (and the workspace is real), the commands the engine runs — the
    #: gates, setup, and scans, i.e. the arbitrary-code-execution surface — are
    #: boxed in a container per this config. git porcelain self-delegates to the
    #: host inside the runner, so it still acts on the real repository. Off by
    #: default; see ``dev_team.sandbox`` and ``docs/SANDBOX.md``.
    sandbox: Optional[SandboxConfig] = None

    def __post_init__(self) -> None:
        if self.max_task_attempts < 1:
            raise ValueError("max_task_attempts must be at least 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.json_retries < 0:
            raise ValueError("json_retries must be non-negative")
        if self.remote_verify_max_polls < 1:
            raise ValueError("remote_verify_max_polls must be at least 1")
        if self.max_replan_rounds < 0:
            raise ValueError("max_replan_rounds must be non-negative")
        if self.retrieval_token_budget < 0:
            raise ValueError("retrieval_token_budget must be non-negative")
        if self.remote_verify_interval_seconds < 0:
            raise ValueError("remote_verify_interval_seconds must be non-negative")
        if self.remote_verify_trigger is not None and self.remote_verify_status is None:
            raise ValueError(
                "remote_verify_trigger requires remote_verify_status "
                "(a trigger with nothing to poll can never pass)"
            )


@dataclass
class DeliveryOutcome:
    """The rich result of a real delivery run."""

    request: FeatureRequest
    plan_summary: str
    design: Design
    task_results: List[TaskResult]
    security: Optional[SecurityReport] = None
    documentation: Optional[Documentation] = None
    reliability: Optional[ReliabilityReport] = None
    deployment: Optional[DeploymentPlan] = None
    blackboard: Optional[Blackboard] = None
    tracer: Optional[Tracer] = None
    budget: Optional[Budget] = None
    workspace_files: List[str] = field(default_factory=list)
    committed: bool = False
    budget_exhausted: bool = False
    resumed_task_ids: List[str] = field(default_factory=list)
    branch: Optional[str] = None
    baseline: Optional[DoDReport] = None
    halted_reason: Optional[str] = None
    scorecard: Dict[str, int] = field(default_factory=dict)
    #: URL of the pull request opened for this delivery, when the caller asked
    #: for one (``--pull-request``) and the delivery had a committed branch to
    #: publish. ``None`` otherwise. Set after ``deliver`` returns, by the
    #: delivery target — the engine itself never opens a PR.
    pull_request_url: Optional[str] = None

    @property
    def tasks_complete(self) -> bool:
        """Whether every task reached a done state."""

        return bool(self.task_results) and all(
            tr.task.status is TaskStatus.DONE for tr in self.task_results
        )

    @property
    def success(self) -> bool:
        """Overall success: tasks done, security approved, production ready."""

        if not self.tasks_complete:
            return False
        # Fail closed: a missing security verdict (the security stage died)
        # means nothing was vetted or committed, so it can never count as a
        # success — the same treatment a block gets at commit time.
        if self.security is None or not self.security.approved:
            return False
        if self.reliability is not None and not self.reliability.production_ready:
            return False
        return True

    @property
    def cost_usd(self) -> float:
        """Total metered cost of the run."""

        return self.budget.spent if self.budget is not None else 0.0


def _dod_to_test_report(report: DoDReport) -> TestReport:
    """Derive a :class:`TestReport` from a Definition-of-Done result."""

    return TestReport(
        passed=report.passed,
        coverage=0.0 if not report.passed else 100.0,
        summary=report.summary(),
    )


def _review_from_dod(report: DoDReport) -> Review:
    """Turn failing gates into reviewer-style feedback for the engineer."""

    return Review(
        approved=False,
        summary=f"Definition of Done not met: {report.summary()}",
        comments=[
            ReviewComment(severity=Severity.MAJOR, message=f"{r.name}: {r.detail}")
            for r in report.failed_gates
        ],
    )


def _prior_context(snapshot: Optional[dict]) -> Optional[str]:
    """Render a compact planning context from a previous run's memory."""

    if not snapshot:
        return None
    lines: List[str] = []
    for decision in snapshot.get("decisions", [])[-5:]:
        lines.append(f"- decision: {decision.get('title')}: {decision.get('decision')}")
    artifacts = snapshot.get("artifacts", [])
    if artifacts:
        lines.append(f"- {len(artifacts)} artifact(s) were produced by earlier runs")
    # Retrospective notes: what went wrong last time, so the plan avoids it.
    for note in (snapshot.get("entries", {}).get("retrospective") or [])[-5:]:
        lines.append(f"- last run: {note}")
    if not lines:
        return None
    return "\n".join(lines)


def _retrospective(
    task_results: List[TaskResult],
    security: Optional[SecurityReport],
) -> List[str]:
    """Distil what went wrong (or was hard) into notes for the next run."""

    notes: List[str] = []
    for tr in task_results:
        if not tr.succeeded:
            detail = ""
            if tr.review is not None and not tr.review.approved:
                detail = f": {tr.review.summary}"
            notes.append(
                f"task {tr.task.id} ({tr.task.title}) failed after "
                f"{tr.attempts} attempt(s){detail}"
            )
        elif tr.attempts > 1:
            notes.append(f"task {tr.task.id} needed {tr.attempts} attempts to pass")
    if security is not None and not security.approved:
        notes.append(f"security blocked the release: {security.summary}")
    return notes


# A snapshot maps path -> prior content, or None when the file did not exist.
_Snapshot = Dict[str, Optional[str]]


class _StashRestoreFailed(Exception):
    """Internal signal: a fail-to-pass stash pop failed to restore the tree.

    Raised inside :meth:`DeliveryEngine._tests_are_vacuous` when the shelved
    implementation could not be popped back cleanly, and handled at its call
    site as a gate failure. It never escapes the engine.
    """

# Written on greenfield agentic deliveries so add-by-path staging (and human
# eyes) never see bytecode, caches, or the engine's own bookkeeping.
_DEFAULT_GITIGNORE = """\
.dev_team/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
node_modules/
.venv/
venv/
.env
*.env
"""

# Paths the engine must keep out of every delivery's git history: its own
# bookkeeping (rollbacks `git clean` it) and local credential files (an
# untracked .env otherwise gets swept into the allow_dirty_baseline commit).
_REQUIRED_IGNORES = (".dev_team/", ".env")


def _gitignore_ignores(existing: str, entry: str) -> bool:
    """Whether ``entry`` is already ignored by a real line in ``existing``.

    Scans non-comment, non-blank lines and treats each as an exact match or a
    glob (via :mod:`fnmatch`) against ``entry`` — and against ``entry`` without
    a trailing slash, so ``.dev_team`` covers ``.dev_team/`` and ``*.env``
    covers ``.env``. Unlike a substring scan it never mistakes a comment or an
    unrelated path for a genuine ignore.
    """

    targets = {entry, entry.rstrip("/")}
    for raw in existing.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.rstrip("/")
        for target in targets:
            if pattern == target or fnmatch.fnmatch(target, pattern):
                return True
    return False


def _branch_slug(title: str) -> str:
    """Derive a git-safe branch segment from a feature title."""

    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in title)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:40] or "feature"


def _plan_to_dict(plan: Plan) -> Dict:
    """Serialise a plan for the checkpoint (identity fields only)."""

    return {
        "summary": plan.summary,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "acceptance_criteria": list(t.acceptance_criteria),
                "dependencies": list(t.dependencies),
            }
            for t in plan.tasks
        ],
    }


def _plan_from_dict(data: Dict) -> Plan:
    """Rebuild a checkpointed plan so a resume works the *same* tasks.

    Regenerating the plan on resume would gamble on the model reproducing
    every task byte-for-byte — the fingerprint match would almost always
    fail and the "resume" would redo everything.
    """

    return Plan(
        summary=str(data.get("summary", "")),
        tasks=[
            Task(
                id=str(t.get("id", "")),
                title=str(t.get("title", "")),
                description=str(t.get("description", "")),
                acceptance_criteria=[str(c) for c in t.get("acceptance_criteria", [])],
                dependencies=[str(d) for d in t.get("dependencies", [])],
            )
            for t in data.get("tasks", [])
        ],
    )


class DeliveryEngine:
    """Delivers a feature for real, with gates, concurrency, and observability."""

    def __init__(
        self,
        runner: AgentRunner,
        *,
        workspace: Optional[Workspace] = None,
        command_runner: Optional[CommandRunner] = None,
        config: Optional[EngineConfig] = None,
        budget: Optional[Budget] = None,
        tracer: Optional[Tracer] = None,
        blackboard: Optional[Blackboard] = None,
        approval: Optional[ApprovalGate] = None,
        policy: Optional[SideEffectPolicy] = None,
        definition_of_done: Optional[DefinitionOfDone] = None,
        git: Optional[GitRepo] = None,
        backlog_store: Optional[BacklogStore] = None,
        checkpoints: Optional[CheckpointStore] = None,
        memory: Optional[ProjectMemory] = None,
        listener: Optional[Listener] = None,
        roster: Optional[Roster] = None,
        interaction: Optional[InteractionChannel] = None,
        transcript_recorder: Optional[TranscriptRecorder] = None,
        engineer_session_factory: Optional[Callable[[], AgentSession]] = None,
    ) -> None:
        self.config = config or EngineConfig()
        # Test seam: builds the raw engineer session (a ClaudeAgentSession in
        # production). The engine wraps whatever this returns in an
        # InstrumentedSession, so metering is identical either way.
        self._engineer_session_factory = engineer_session_factory
        self.workspace: Workspace = workspace or InMemoryWorkspace()
        self.budget = budget or Budget()
        self.tracer = tracer or Tracer()
        self.blackboard = blackboard or Blackboard()
        self.approval = approval or AutoApprover()
        self.listener = listener
        self.roster = roster if roster is not None else Roster.default()
        self.interaction = interaction
        self.transcript_recorder = transcript_recorder

        # Everything side-effecting is rooted at the workspace, never at the
        # orchestrator's own working directory.
        root = getattr(self.workspace, "root", None)
        self.workdir: Optional[str] = str(root) if root is not None else None

        if command_runner is None:
            # An in-memory workspace has nothing on disk to run against, so it
            # pairs with an honest dry-run runner rather than real subprocesses
            # aimed at the wrong directory.
            command_runner = (
                SubprocessCommandRunner(cwd=self.workdir)
                if self.workdir is not None
                else DryRunCommandRunner()
            )
        if self.config.sandbox is not None and self.workdir is not None:
            # Box the code the engine runs (gates/setup/scans) in a container;
            # git self-delegates to the host inside ContainerCommandRunner, so
            # porcelain still acts on the real repo. Only with a real workspace
            # — an in-memory/dry-run has nothing on disk to run against or mount.
            command_runner = ContainerCommandRunner(command_runner, self.config.sandbox)
        self.command_runner: CommandRunner = GuardedCommandRunner(
            command_runner,
            policy=policy or SideEffectPolicy(),
            approval=self.approval,
        )
        self.git = git if git is not None else GitRepo(self.command_runner, cwd=self.workdir)
        # Committing requires somewhere real to commit: either the caller gave
        # us a GitRepo on purpose, or the workspace has a root.
        self._can_commit = self.config.commit and (git is not None or self.workdir is not None)

        self.agentic = (
            self.config.agentic
            if self.config.agentic is not None
            else self.workdir is not None
        )
        if self.agentic and self.workdir is None:
            raise ValueError("agentic mode requires a workspace with a real root directory")
        if self.config.worktrees and not self.agentic:
            raise ValueError("worktrees require agentic mode (a workspace with a real root)")
        self._use_worktrees = self.config.worktrees

        self.change_applier = ChangeApplier(self.workspace)
        # Whether the gates genuinely exercise the change on this machine.
        # Remote CI and degraded (evidence-only) verification both set this
        # False, which disables the fail-to-pass check: re-running such
        # "gates" against reverted code proves nothing about the tests.
        self._local_verification = True
        # Gate resolution: an injected DoD wins; an explicit verify_command
        # builds one; a configured remote CI verification comes next;
        # otherwise the command is auto-detected from the workspace at
        # deliver time (after setup, when manifests exist).
        if definition_of_done is not None:
            self.definition_of_done: Optional[DefinitionOfDone] = definition_of_done
        elif self.config.verify_command is not None:
            self.definition_of_done = DefinitionOfDone(
                [CommandGate("tests", self.config.verify_command)]
            )
        elif self.config.remote_verify_status is not None:
            self.definition_of_done = DefinitionOfDone([self._remote_ci_gate()])
            self._local_verification = False
        else:
            self.definition_of_done = None
        # A real workspace gets a persistent backlog by default; without one
        # the "persistent backlog the engine records every run into" would be
        # wired to nothing.
        self.backlog_store = backlog_store or (
            BacklogStore(self.workspace) if self.workdir is not None else None
        )
        self.memory = memory or ProjectMemory(self.workspace)
        self.checkpoints = checkpoints or (
            CheckpointStore(self.workspace) if self.config.resume else None
        )
        self._integration_lock = asyncio.Lock()
        # git stash is one stack shared by every worktree of a repo; without
        # serialising push/pop pairs, concurrent fail-to-pass checks pop each
        # other's stashes and restore implementations into the wrong worktree.
        self._stash_lock = asyncio.Lock()
        self._checkpoint: Optional[RunCheckpoint] = None
        self._budget_exhausted = False
        self._branch: Optional[str] = None
        self._baseline_failures = None
        self._baseline_sha: Optional[str] = None
        self._profile = None
        self._conventions: Optional[str] = None
        self._scorecard: Dict[str, int] = {}

        def make(cls):
            wrapped = InstrumentedRunner(
                runner,
                cls.role,
                budget=self.budget,
                tracer=self.tracer,
                transcript_recorder=self.transcript_recorder,
            )
            return cls(
                wrapped,
                model=self.config.role_models.get(cls.role, self.config.model),
                listener=listener,
                json_retries=self.config.json_retries,
                persona=self.roster.get(cls.role),
            )

        self.manager = make(ProductManagerAgent)
        self.architect = make(ArchitectAgent)
        self.engineer = make(EngineerAgent)
        self.reviewer = make(ReviewerAgent)
        self.qa = make(QAAgent)
        self.security = make(SecurityEngineerAgent)
        self.writer = make(TechnicalWriterAgent)
        self.sre = make(SREAgent)
        self.devops = make(DevOpsAgent)

    def _event(self, stage: str, message: str, detail: Optional[str] = None) -> None:
        emit(
            self.listener,
            AgentEvent(role="engine", stage=stage, message=message, detail=detail),
        )

    async def deliver(self, request: FeatureRequest) -> DeliveryOutcome:
        """Run the full real delivery lifecycle for ``request``."""

        run_span = self.tracer.start("workflow", "deliver", feature=request.title)
        self._event("start", f"Delivering: {request.title}")
        self._budget_exhausted = False
        self._branch = None
        self._baseline_sha = None

        if self.agentic or (self.workdir is not None and self._can_commit):
            # Described-mode deliveries to a real directory need the same
            # safeguards as agentic ones — dirty-tree halt, dedicated delivery
            # branch, baseline commit — or the run would sweep pre-existing
            # uncommitted work into a commit on whatever branch is checked out.
            halted = self._prepare_git_baseline(request)
            if halted is not None:
                self.tracer.end(run_span, "halted")
                return halted

        if self.config.setup_command is not None:
            result = self.command_runner.run(
                list(self.config.setup_command), cwd=self.workdir
            )
            if not result.ok:
                self.tracer.end(run_span, "halted")
                return self._halted(
                    request,
                    f"setup command failed ({result.exit_code}): {result.output[:500]}",
                )

        self._profile = detect_project(self.workspace)
        self._resolve_gates()
        self._scorecard: Dict[str, int] = {
            "plan_lint_issues": 0,
            "review_rejections": 0,
            "gate_failures": 0,
            "vacuous_test_rejections": 0,
        }

        self._baseline_failures = None
        baseline = await self._check_baseline()
        if baseline is not None and not baseline.passed:
            if self.config.require_green_baseline:
                self.tracer.end(run_span, "halted")
                return self._halted(
                    request,
                    "baseline quality gates are already failing — fix the existing "
                    f"breakage first or set require_green_baseline=False "
                    f"({baseline.summary()})",
                    baseline=baseline,
                )
            if self.config.tolerate_baseline_failures:
                self._baseline_failures = parse_failed_tests(
                    "\n".join(g.detail for g in baseline.failed_gates)
                )
                if self._baseline_failures is not None:
                    self._event(
                        "baseline",
                        f"Tolerating {len(self._baseline_failures)} pre-existing "
                        "failing test(s); tasks are gated on new failures only",
                    )

        repo_ctx = build_repo_context(self.workspace)
        snapshot_memory = self.memory.load()
        # A stored conventions profile (captured by an assessment run) makes
        # "follows the house style" part of implementation and review.
        stored_conventions = ConventionsStore(self.workspace).load()
        self._conventions = stored_conventions.render() if stored_conventions else None
        if self._conventions:
            self._event(
                "conventions",
                "House conventions profile loaded; engineer and reviewer will follow it",
            )
        # Continue ADR numbering where earlier runs stopped, so the persisted
        # decision log never collides ids.
        self.blackboard.seed_decision_ids(
            len((snapshot_memory or {}).get("decisions", []))
        )

        self._checkpoint = (
            self.checkpoints.load(request.title) if self.checkpoints is not None else None
        )
        resuming = self._checkpoint is not None and bool(self._checkpoint.done_task_ids)
        if resuming and self._checkpoint.baseline_sha and self._baseline_sha is not None:
            # Squash from the interrupted run's baseline so the final feature
            # commit spans that run's banked work too.
            self._baseline_sha = self._checkpoint.baseline_sha

        context_parts = [
            part
            for part in (_prior_context(snapshot_memory), repo_ctx.render() or None)
            if part
        ]
        prior = "\n\n".join(context_parts) if context_parts else None
        try:
            if resuming and self._checkpoint.plan is not None:
                plan = _plan_from_dict(self._checkpoint.plan)
                self._event(
                    "resumed",
                    "Reusing the checkpointed plan from the interrupted run",
                )
            else:
                plan = await self.manager.create_plan(request, prior_context=prior)
                issues = lint_plan(plan)
                if issues:
                    # One INVEST-lint revision pass: a plan QA can't verify or the
                    # scheduler can't order wastes every downstream agent's budget.
                    self._scorecard["plan_lint_issues"] += len(issues)
                    self._event(
                        "plan-lint", f"Plan failed lint ({len(issues)} issue(s)); revising"
                    )
                    plan = await self.manager.create_plan(
                        request,
                        prior_context=prior,
                        revision_feedback="\n".join(f"- {i}" for i in issues),
                    )
                    remaining = lint_plan(plan)
                    if remaining:
                        self._event(
                            "plan-lint",
                            f"Plan still has {len(remaining)} lint issue(s); proceeding anyway",
                            detail="; ".join(remaining[:5]),
                        )
                self._dedupe_task_ids(plan.tasks)
            self.blackboard.post_artifact("plan", "plan", plan.summary)
            self._event("planned", "Plan ready", detail=f"{len(plan.tasks)} task(s)")

            if not resuming:
                # A checkpointed plan was already approved by the run that
                # created it; re-litigating it on resume would let a second
                # approval drift from the banked work.
                plan = await self._review_plan(request, plan, prior)
                if plan is None:
                    self.tracer.end(run_span, "halted")
                    return self._halted(request, "plan rejected at interactive review")

            prior_decisions = [
                f"{d.get('title')}: {d.get('decision')}"
                for d in (snapshot_memory or {}).get("decisions", [])
            ]
            design = await self.architect.design(
                request,
                plan,
                repo_context=repo_ctx.render() or None,
                relevant_code=self._retrieve_context(
                    f"{request.title}\n{request.description}"
                ),
                prior_decisions=prior_decisions or None,
            )
        except BudgetExceededError:
            self.tracer.end(run_span, "halted")
            return self._halted(request, "budget exhausted before any task work began")
        except DevTeamError as exc:
            # A run that dies during planning/design must still return an
            # outcome (with trace and cost) rather than unwind the caller.
            self.tracer.end(run_span, "halted")
            return self._halted(request, f"planning failed: {exc}")
        self.blackboard.record_decision(
            title=f"Architecture for {request.title}",
            context=request.description,
            decision=design.overview,
            consequences=design.rationale,
        )
        self._event("designed", "Design ready")

        if self._checkpoint is not None:
            if not self._checkpoint.baseline_sha:
                self._checkpoint.baseline_sha = self._baseline_sha
            self._checkpoint.plan = _plan_to_dict(plan)

        backlog, stories = self._register_backlog(request, plan.tasks)

        resumed: List[str] = []
        results: Dict[str, TaskResult] = {}
        pending: List[Task] = []
        for task in plan.tasks:
            fingerprint = task_fingerprint(task.title, task.description)
            if self._checkpoint is not None and self._checkpoint.is_done(
                task.id, fingerprint
            ):
                task.status = TaskStatus.DONE
                results[task.id] = TaskResult(task=task, attempts=0)
                resumed.append(task.id)
            else:
                pending.append(task)
        if resumed:
            self._event("resumed", f"Restored {len(resumed)} task(s) from checkpoint")

        async def worker(task: Task) -> bool:
            # Idempotent by task id: an already-attempted task reports its prior
            # outcome instead of re-running. This lets a re-plan round hand the
            # scheduler the *whole* plan (so a still-failed prerequisite stays in
            # the graph and correctly cascade-skips its dependents) while the
            # done/failed tasks are no-ops rather than re-executions.
            existing = results.get(task.id)
            if existing is not None:
                return existing.succeeded
            if self._budget_exhausted or self.budget.exhausted:
                task.status = TaskStatus.FAILED
                results[task.id] = TaskResult(task=task, attempts=0)
                return False
            try:
                outcome = await self._develop_task(task, design)
            except BudgetExceededError:
                self._budget_exhausted = True
                task.status = TaskStatus.FAILED
                outcome = TaskResult(task=task, attempts=0)
                self._event("budget", f"Budget exhausted during {task.id}")
            results[task.id] = outcome
            return outcome.succeeded

        try:
            await schedule(
                pending,
                worker,
                max_concurrency=self.config.max_concurrency,
                listener=self._on_scheduled,
            )
        except DependencyCycleError as exc:
            # lint_plan catches cycles pre-flight, but a plan can still slip
            # through (a revision that stays cyclic, or a resumed checkpoint).
            # A cycle must not unwind deliver() and lose the outcome, trace,
            # and checkpoint: mark the un-run tasks FAILED (handled by the
            # task_results loop below), record it, and finish gracefully.
            self._event(
                "cycle",
                "Plan has a dependency cycle; remaining tasks cannot run",
                detail=str(exc),
            )

        plan = await self._replan_loop(request, plan, results, worker)

        task_results: List[TaskResult] = []
        for task in plan.tasks:
            if task.id in results:
                task_results.append(results[task.id])
            else:  # cascade-skipped by the scheduler
                task.status = TaskStatus.FAILED
                task_results.append(TaskResult(task=task, attempts=0))

        security = await self._specialist(self._security_review(request, task_results))
        deployment = await self._specialist(
            self._provision_deployment(request, design)
        )
        reliability = await self._specialist(
            self._assess_reliability(request, design, task_results, deployment)
        )
        documentation = await self._specialist(
            self._write_documentation(request, design, task_results)
        )

        committed = self._commit_if_approved(request, task_results, security)
        self._finalise_backlog(backlog, stories, task_results)
        notes = _retrospective(task_results, security)
        if notes:
            self.blackboard.put("retrospective", notes)
        self.blackboard.put("scorecard", dict(self._scorecard))
        self.memory.save(self.blackboard)

        self.tracer.end(run_span)
        outcome = DeliveryOutcome(
            request=request,
            plan_summary=plan.summary,
            design=design,
            task_results=task_results,
            security=security,
            documentation=documentation,
            reliability=reliability,
            deployment=deployment,
            blackboard=self.blackboard,
            tracer=self.tracer,
            budget=self.budget,
            workspace_files=[
                f for f in self.workspace.list_files() if not f.startswith(_INTERNAL_PREFIX)
            ],
            committed=committed,
            budget_exhausted=self._budget_exhausted,
            resumed_task_ids=resumed,
            branch=self._branch,
            baseline=baseline,
            scorecard=dict(self._scorecard),
        )
        if outcome.success and self.checkpoints is not None:
            self.checkpoints.clear(request.title)
        verdict = "succeeded" if outcome.success else "with issues"
        self._event("done", f"Delivery finished {verdict}", detail=f"${outcome.cost_usd:.4f}")
        return outcome

    # -- run preparation -------------------------------------------------------

    def _prepare_git_baseline(self, request: FeatureRequest) -> Optional[DeliveryOutcome]:
        """Get the repo ready for delivery work; return a halted outcome if unsafe.

        Ensures a repo exists, refuses to run over uncommitted work (unless
        explicitly allowed), makes sure ``.dev_team/`` is git-ignored, switches
        to a dedicated delivery branch, and commits the baseline the delivery
        squashes onto (and that failed attempts roll back to).
        """

        self.git.ensure_repo()
        # The engine's own bookkeeping never counts as the *user's* dirt.
        dirty = [
            p for p in self.git.changed_files() if not p.startswith(_INTERNAL_PREFIX)
        ]
        if dirty and not self.config.allow_dirty_baseline:
            return self._halted(
                request,
                "the working tree has uncommitted changes; commit or stash them "
                "first, or set allow_dirty_baseline=True to sweep them into a "
                "baseline commit on the delivery branch",
            )
        self._ensure_gitignore()
        if self.config.use_branch:
            self._branch = self.config.branch or f"dev-team/{_branch_slug(request.title)}"
            self.git.switch_to(self._branch)
        if self.git.has_changes():
            self.git.add_all()
            self.git.commit("chore(dev-team): baseline before delivery")
        # WIP commits and the final squash both need a baseline commit to
        # exist and its exact sha. An unresolvable sha (e.g. a stubbed runner)
        # disables baseline tracking rather than corrupting it.
        if not self.git.has_commits():
            self.git.commit("chore(dev-team): init", allow_empty=True)
        self._baseline_sha = self.git.rev_parse("HEAD") or None
        return None

    def _ensure_gitignore(self) -> None:
        """Make sure ``.dev_team/`` and ``.env`` are ignored, whoever authored it.

        Rollbacks run ``git clean -fd``; if the bookkeeping directory is not
        ignored, every rollback deletes the checkpoint and memory files
        mid-run, and the leftovers read as a dirty tree on the next run.
        Ignoring ``.env`` keeps a stray local secret file out of the baseline
        commit (this runs before the allow_dirty_baseline ``add_all()``).
        """

        if not self.config.write_gitignore:
            return
        if not self.workspace.exists(".gitignore"):
            self.workspace.write_text(".gitignore", _DEFAULT_GITIGNORE)
            return
        existing = self.workspace.read_text(".gitignore")
        # A substring scan ('.dev_team' in existing) is fragile: it matches a
        # comment or an unrelated path and misses nothing being genuinely
        # ignored. Check real ignore lines instead.
        missing = [e for e in _REQUIRED_IGNORES if not _gitignore_ignores(existing, e)]
        if missing:
            separator = "" if existing.endswith("\n") else "\n"
            added = "".join(f"{entry}\n" for entry in missing)
            self.workspace.write_text(
                ".gitignore",
                f"{existing}{separator}# dev-team: internal bookkeeping and local secrets\n"
                f"{added}",
            )

    def _resolve_gates(self) -> None:
        """Build the Definition of Done from the workspace when not configured."""

        if self.definition_of_done is not None:
            return
        profile = self._profile or detect_project(self.workspace)
        self.blackboard.put("project_profile", profile.kind)
        if profile.verify_command is None:
            # The stack was recognised but cannot build or test on this
            # machine (e.g. legacy .NET Framework). Running any local command
            # would fail every task for reasons no engineer can fix, so
            # verification degrades to an always-pass marker gate: review,
            # security, and static findings become the quality bar, and the
            # fail-to-pass check is disabled as meaningless.
            self._local_verification = False
            self.definition_of_done = DefinitionOfDone(
                [
                    PredicateGate(
                        "verification-unavailable",
                        lambda _ctx: True,
                        detail=(
                            f"{profile.kind} is not locally runnable "
                            f"({profile.reason}); relying on evidence-based "
                            "review — configure remote_verify_status to gate "
                            "on your real CI instead"
                        ),
                    )
                ]
            )
            self._event(
                "gates",
                f"Auto-detected {profile.kind} project — no local verify command",
                detail=(
                    f"{profile.reason}; verification degraded to evidence-based "
                    "review (set remote_verify_status to gate on real CI)"
                ),
            )
            return
        self.definition_of_done = DefinitionOfDone(
            [CommandGate("tests", profile.verify_command)]
        )
        self._event(
            "gates",
            f"Auto-detected {profile.kind} project",
            detail=f"verify: {' '.join(profile.verify_command)} ({profile.reason})",
        )

    def _remote_ci_gate(self) -> RemoteCIGate:
        """The configured external-CI gate (requires remote_verify_status)."""

        return RemoteCIGate(
            "remote-ci",
            self.config.remote_verify_status,
            trigger_command=self.config.remote_verify_trigger,
            max_polls=self.config.remote_verify_max_polls,
            poll_interval_seconds=self.config.remote_verify_interval_seconds,
        )

    def _gate_context(
        self, task: Optional[Task] = None, *, cwd: Optional[str] = None
    ) -> GateContext:
        return GateContext(
            runner=self.command_runner,
            workspace=self.workspace,
            task=task,
            cwd=cwd if cwd is not None else self.workdir,
            timeout=self.config.gate_timeout_seconds,
        )

    async def _check_baseline(self) -> Optional[DoDReport]:
        """Evaluate the gates before any work starts, when there is anything
        to evaluate.

        An empty workspace (greenfield) has no baseline to check — and a
        ``.gitignore`` the engine itself just authored doesn't make it any
        less empty. On a populated workspace this is what separates inherited
        breakage from breakage the team introduces — without it, a legacy
        repo's one flaky test fails every task and the engineer gets blamed
        for code it never touched.

        The evaluation runs off the event loop: a remote-CI gate polls with
        blocking ``time.sleep`` for up to its whole timeout (~1800s), which
        would otherwise starve the loop (and every concurrent task) here just
        as it does at per-task integration time.
        """

        product_files = [
            f
            for f in self.workspace.list_files()
            if f != ".gitignore" and not f.startswith(_INTERNAL_PREFIX)
        ]
        if not product_files:
            return None
        report = await asyncio.to_thread(
            self.definition_of_done.evaluate, self._gate_context()
        )
        status = "green" if report.passed else "RED"
        self._event("baseline", f"Baseline gates: {status}", detail=report.summary())
        return report

    def _halted(
        self,
        request: FeatureRequest,
        reason: str,
        *,
        baseline: Optional[DoDReport] = None,
    ) -> DeliveryOutcome:
        """Build the outcome for a run stopped before any task work began."""

        self._event("halted", f"Delivery halted: {reason}")
        return DeliveryOutcome(
            request=request,
            plan_summary="",
            design=Design(overview=""),
            task_results=[],
            blackboard=self.blackboard,
            tracer=self.tracer,
            budget=self.budget,
            workspace_files=[
                f for f in self.workspace.list_files() if not f.startswith(_INTERNAL_PREFIX)
            ],
            branch=self._branch,
            baseline=baseline,
            halted_reason=reason,
        )

    async def _specialist(self, coro):
        """Run a post-task specialist stage, degrading gracefully on failure.

        A specialist that cannot produce a verdict (budget, persistent SDK or
        parse failure) yields ``None`` instead of unwinding a run whose task
        work already succeeded — and a missing security verdict already fails
        closed at commit time.
        """

        try:
            return await coro
        except BudgetExceededError:
            self._budget_exhausted = True
            self._event("budget", "Budget exhausted; skipping remaining specialist stages")
            return None
        except DevTeamError as exc:
            self._event("specialist", "Specialist stage failed", detail=str(exc))
            return None

    def _on_scheduled(self, result: ScheduledResult) -> None:
        detail = result.error
        self._event("scheduled", f"{result.task_id} {result.status.value}", detail=detail)

    # -- task development ----------------------------------------------------

    def _attempt_model(self, attempts: int) -> Optional[str]:
        """The model for this attempt (escalation on the final one)."""

        if attempts == self.config.max_task_attempts:
            return self.config.escalation_model
        return None

    def _retrieve_context(self, query: str) -> Optional[str]:
        """The workspace's most-relevant code for ``query`` as a prompt block.

        ``None`` unless retrieval is enabled. Deterministic lexical ranking
        bounded by the per-role token budget; the amount pulled in is logged so
        the added context is never silent.
        """

        if not self.config.retrieval:
            return None
        result = retrieve(
            self.workspace,
            query,
            char_budget=char_budget_for_tokens(self.config.retrieval_token_budget),
        )
        if result.is_empty:
            return None
        block = result.render()
        self._event(
            "retrieval",
            f"Retrieved {len(result.files)} relevant file(s) of {result.considered}",
            detail=f"~{estimate_tokens(block)} tokens of context",
        )
        return block

    async def _review_plan(
        self, request: FeatureRequest, plan: Plan, prior: Optional[str]
    ) -> Optional[Plan]:
        """Interactive plan review: approve, revise (repeatedly), or abort.

        Returns the approved plan, or ``None`` when the human aborts. Without
        an interaction channel the plan passes straight through.
        """

        if self.interaction is None:
            return plan
        asked_by = self.roster.display_name("product-manager")
        while True:
            reply = await ask_in_thread(
                self.interaction, plan_review_question(plan, asked_by=asked_by)
            )
            if reply.choice == "approve":
                self._event("plan-approved", "Plan approved interactively")
                return plan
            if reply.choice == "abort":
                self._event("plan-review", "Plan rejected; aborting the run")
                return None
            self._event(
                "plan-review", "Plan revision requested", detail=reply.text or None
            )
            plan = await self.manager.create_plan(
                request,
                prior_context=prior,
                revision_feedback=reply.text or "Revise the plan.",
            )
            self._dedupe_task_ids(plan.tasks)
            self._event(
                "planned", "Revised plan ready", detail=f"{len(plan.tasks)} task(s)"
            )

    def _failure_evidence(self, result: Optional[TaskResult]) -> str:
        """A compact, human-readable summary of why a task failed.

        Feeds both the interactive retry escalation and the re-planner, so the
        manager (or the human) sees the same review/test evidence.
        """

        parts = []
        if result is not None:
            if result.review is not None:
                parts.append(f"review: {result.review.summary}")
            if result.test_report is not None:
                parts.append(f"tests: {result.test_report.summary}")
        return "\n".join(parts) or "no evidence captured"

    async def _replan_loop(
        self,
        request: FeatureRequest,
        plan: Plan,
        results: Dict[str, TaskResult],
        worker,
    ) -> Plan:
        """Recover still-failed tasks by mutating the plan and re-scheduling.

        Off unless ``config.max_replan_rounds`` > 0. Each round the manager
        proposes a mutation (split/replace/drop) for every still-failed task; a
        human supervises it through the interaction channel when one is
        attached, otherwise it applies autonomously. The mutated plan's
        not-yet-attempted tasks (the replacements and any dependents unblocked
        by the change) are re-scheduled through the same ``worker``, so their
        results land in ``results`` exactly as the first pass did. Bounded by
        the round count and the budget; returns the final (possibly mutated)
        plan.
        """

        rounds = self.config.max_replan_rounds
        while rounds > 0 and not (self._budget_exhausted or self.budget.exhausted):
            failed = [
                t for t in plan.tasks
                if t.id in results and not results[t.id].succeeded
            ]
            if not failed:
                break
            mutated = False
            for task in failed:
                decision = await self._propose_replan(request, plan, task, results)
                if self._budget_exhausted:
                    break  # budget died mid-round; remaining tasks would only repeat it
                if decision is None:
                    continue
                try:
                    plan = apply_replan(plan, decision)
                except ReplanError as exc:
                    self._event(
                        "replan",
                        f"Discarded an invalid re-plan for {task.id}",
                        detail=str(exc),
                    )
                    continue
                mutated = True
                self._event(
                    "replan",
                    f"Re-planned {task.id}: {decision.action.value}",
                    detail=decision.rationale or None,
                )
            if not mutated:
                break
            # Persist the mutated plan *before* rescheduling, so a crash mid-round
            # resumes on the new plan (matching the replacement tasks that
            # _record_progress marks done) rather than the stale pre-re-plan one.
            self._checkpoint_plan(plan)
            rounds -= 1
            if not any(t.id not in results for t in plan.tasks):
                break  # nothing new to attempt this round
            # Reschedule the WHOLE plan, not just the new tasks: the worker
            # no-ops already-attempted ids, and passing the full graph keeps
            # every dependency edge intact so a dependent of a still-failed task
            # is cascade-skipped rather than run on work that never succeeded.
            # No DependencyCycleError guard: apply_replan re-lints every mutation,
            # so the plan (and its scheduled subset) is always acyclic.
            await schedule(
                plan.tasks,
                worker,
                max_concurrency=self.config.max_concurrency,
                listener=self._on_scheduled,
            )
        return plan

    def _checkpoint_plan(self, plan: Plan) -> None:
        """Persist ``plan`` into the resume checkpoint (best effort)."""

        if self._checkpoint is not None and self.checkpoints is not None:
            self._checkpoint.plan = _plan_to_dict(plan)
            self.checkpoints.save(self._checkpoint)

    async def _propose_replan(
        self,
        request: FeatureRequest,
        plan: Plan,
        task: Task,
        results: Dict[str, TaskResult],
    ) -> Optional[Replan]:
        """Get a re-plan decision for one failed task, or ``None`` to leave it.

        The manager proposes; when an interaction channel is attached a human
        supervises (apply / revise-with-text / reject), otherwise the proposal
        applies autonomously. Budget exhaustion stops re-planning.
        """

        evidence = self._failure_evidence(results.get(task.id))
        feedback: Optional[str] = None
        while True:
            try:
                decision = await self.manager.replan(
                    request, plan, task, evidence, revision_feedback=feedback
                )
            except BudgetExceededError:
                self._budget_exhausted = True
                self._event("budget", f"Budget exhausted re-planning {task.id}")
                return None
            if self.interaction is None:
                return decision  # autonomous: apply the manager's proposal
            reply = await ask_in_thread(
                self.interaction,
                replan_review_question(
                    decision, asked_by=self.roster.display_name("product-manager")
                ),
            )
            if reply.choice == "apply":
                return decision
            if reply.choice == "reject":
                self._event("replan", f"Re-plan for {task.id} rejected; left failed")
                return None
            feedback = reply.text or "Propose a different re-plan."
            self._event(
                "replan", f"Re-plan revision requested for {task.id}", detail=feedback
            )

    async def _escalate_failure(
        self, task: Task, result: TaskResult
    ) -> Optional[Review]:
        """Ask the human what to do with a task that failed all attempts.

        Returns guidance (as reviewer-style feedback for a fresh attempt
        round) when the human chooses to retry, else ``None`` to accept the
        failure. Unattended runs (no channel) and exhausted budgets always
        accept the failure.
        """

        if self.interaction is None or self.budget.exhausted:
            return None
        reply = await ask_in_thread(
            self.interaction,
            task_failure_question(
                task.id,
                self._failure_evidence(result),
                asked_by=self.roster.display_name("engineer"),
            ),
        )
        if reply.choice != "retry":
            self._event("task-failed", f"{task.id} failure accepted interactively")
            return None
        guidance = reply.text or "Please try a different approach."
        self._event("task-retry", f"Retrying {task.id} with guidance", detail=guidance)
        return Review(
            approved=False,
            summary=f"Human guidance for the retry: {guidance}",
            comments=[ReviewComment(severity=Severity.MAJOR, message=guidance)],
        )

    async def _develop_task(self, task: Task, design: Design) -> TaskResult:
        """Develop ``task``, escalating exhausted attempts to the human."""

        feedback: Optional[Review] = None
        total_attempts = 0
        while True:
            result = await self._attempt_task(task, design, feedback)
            total_attempts += result.attempts
            result.attempts = total_attempts
            if result.succeeded:
                return result
            feedback = await self._escalate_failure(task, result)
            if feedback is None:
                return result

    async def _attempt_task(
        self, task: Task, design: Design, initial_feedback: Optional[Review] = None
    ) -> TaskResult:
        if self._use_worktrees:
            return await self._develop_task_in_worktree(task, design, initial_feedback)

        feedback: Optional[Review] = initial_feedback
        implementation: Optional[Implementation] = None
        review: Optional[Review] = None
        test_report: Optional[TestReport] = None
        attempts = 0
        # One persistent session per task (opt-in), reused across attempts so a
        # retry continues rather than restarts cold. None when off or not
        # agentic; closed in the finally.
        session = self._open_engineer_session()

        try:
            while attempts < self.config.max_task_attempts:
                attempts += 1
                model = self._attempt_model(attempts)
                task.status = TaskStatus.IN_PROGRESS
                span = self.tracer.start("task", task.id, attempt=str(attempts))

                if self.agentic:
                    # The agentic engineer mutates the shared working directory,
                    # so the whole attempt runs inside the integration lock.
                    async with self._integration_lock:
                        try:
                            implementation, session = await self._engineer_attempt(
                                task, design, feedback, session,
                                continued=attempts > 1, model=model,
                            )
                        except BaseException:
                            # The engineer edits the shared workdir directly. A
                            # raising call (AgentResponseError, budget, cancel)
                            # leaves those edits on disk outside any rollback
                            # scope — _integrate never runs. Discard them here,
                            # or the next task's _commit_wip banks this failed
                            # task's half-written changes as gated work.
                            self._rollback(None, self.git)
                            raise
                        done, review, test_report, feedback = await self._integrate(
                            task, implementation, span
                        )
                        if done:
                            self._commit_wip(task)
                else:
                    implementation, done, review, test_report, feedback = (
                        await self._attempt_described(task, design, feedback, model, span)
                    )

                if done:
                    task.status = TaskStatus.DONE
                    self._record_progress(task)
                    self.tracer.end(span, "done")
                    return TaskResult(task, attempts, implementation, review, test_report)

            task.status = TaskStatus.FAILED
            return TaskResult(task, attempts, implementation, review, test_report)
        finally:
            if session is not None:
                await session.aclose()

    async def _attempt_described(self, task, design, feedback, model, span):
        """One described-mode attempt (in-memory / dry-run; no session).

        Returns ``(implementation, done, review, test_report, feedback)`` for
        the caller's loop to act on.
        """

        implementation = await self.engineer.implement(
            task,
            design,
            feedback,
            workspace_listing=[
                f
                for f in self.workspace.list_files()
                if not f.startswith(_INTERNAL_PREFIX)
            ],
            conventions=self._conventions,
            relevant_code=self._retrieve_context(
                "\n".join([task.title, task.description, *task.acceptance_criteria])
            ),
            model=model,
        )
        async with self._integration_lock:
            done, review, test_report, feedback = await self._integrate(
                task, implementation, span
            )
            if done:
                self._commit_wip(task)
        return implementation, done, review, test_report, feedback

    def _open_engineer_session(self) -> Optional[AgentSession]:
        """Open an instrumented engineer session for a task, or ``None``.

        ``None`` unless ``reuse_engineer_session`` is set and the run is agentic
        (the described/in-memory path has no real workspace to run tools in).
        The raw session comes from the injected factory (tests) or a
        :class:`ClaudeAgentSession` fixed to the engineer's system prompt,
        tools, cwd, and model; it is wrapped in an :class:`InstrumentedSession`
        so every turn meters and traces exactly like a runner call.
        """

        if not (self.config.reuse_engineer_session and self.agentic):
            return None
        if self._engineer_session_factory is not None:
            inner: AgentSession = self._engineer_session_factory()
        else:
            tools = (
                list(self.config.engineer_tools)
                if self.config.engineer_tools is not None
                else list(ENGINEER_TOOLS)
            )
            inner = ClaudeAgentSession(
                system_prompt=self.engineer.effective_system_prompt,
                allowed_tools=tools,
                model=self.engineer.model,
                cwd=str(self.workdir),
            )
        return InstrumentedSession(
            inner,
            "engineer",
            budget=self.budget,
            tracer=self.tracer,
            transcript_recorder=self.transcript_recorder,
            system_prompt=self.engineer.effective_system_prompt,
        )

    async def _engineer_attempt(self, task, design, feedback, session, *, continued, model):
        """Run one engineer attempt, over the session when there is one.

        Returns ``(implementation, session)``. A session turn that fails
        (:class:`AgentResponseError`, e.g. the persistent client wedged) is not
        fatal: the session is discarded and the attempt retried once on the
        proven cold :meth:`implement_in_place` path — so ``session`` comes back
        ``None`` and every later attempt stays cold. Per-attempt model
        escalation applies only to that cold path; the session's model is fixed.
        """

        if session is not None:
            try:
                implementation = await self.engineer.implement_over_session(
                    session, task, design, feedback,
                    conventions=self._conventions, continued=continued,
                )
                return implementation, session
            except AgentResponseError:
                await session.aclose()
                self._event(
                    "engineer",
                    f"Engineer session failed for {task.id}; falling back to a cold attempt",
                )
                session = None
        implementation = await self.engineer.implement_in_place(
            task,
            design,
            feedback,
            cwd=str(self.workdir),
            conventions=self._conventions,
            model=model,
            tools=self.config.engineer_tools,
        )
        return implementation, session

    async def _develop_task_in_worktree(
        self,
        task: Task,
        design: Design,
        initial_feedback: Optional[Review] = None,
    ) -> TaskResult:
        """Develop ``task`` in its own git worktree, merging only when green.

        Implementation, review, and gate runs all happen inside the task's
        worktree — in parallel with other tasks. Only the squash-merge into
        the delivery branch (plus a full gate check on the merged state) is
        serialised.
        """

        wt_path = f"{self.workdir}/.dev_team/worktrees/{task.id.lower()}"
        task_branch = f"{self._branch or 'dev-team'}-task-{task.id.lower()}"
        async with self._integration_lock:  # worktree creation mutates .git
            # A crashed run can leave the worktree and branch behind; clear
            # them so the rerun's task doesn't fail on arrival.
            self.git.worktree_remove(wt_path)
            self.git.worktree_prune()
            self.git.worktree_add(wt_path, task_branch)
        arena_ws = LocalWorkspace(wt_path)
        arena_git = GitRepo(self.command_runner, cwd=wt_path)

        feedback: Optional[Review] = initial_feedback
        implementation: Optional[Implementation] = None
        review: Optional[Review] = None
        test_report: Optional[TestReport] = None
        attempts = 0
        try:
            while attempts < self.config.max_task_attempts:
                attempts += 1
                task.status = TaskStatus.IN_PROGRESS
                span = self.tracer.start("task", task.id, attempt=str(attempts))

                implementation = await self.engineer.implement_in_place(
                    task,
                    design,
                    feedback,
                    cwd=wt_path,
                    conventions=self._conventions,
                    model=self._attempt_model(attempts),
                    tools=self.config.engineer_tools,
                )
                done, review, test_report, feedback = await self._integrate(
                    task,
                    implementation,
                    span,
                    workspace=arena_ws,
                    git=arena_git,
                    cwd=wt_path,
                )
                if not done:
                    continue

                merged, merge_feedback = await self._merge_task(task, arena_git, task_branch)
                if merged:
                    task.status = TaskStatus.DONE
                    self._record_progress(task)
                    self.tracer.end(span, "done")
                    return TaskResult(task, attempts, implementation, review, test_report)
                feedback = merge_feedback
                self.tracer.end(span, "merge-gates-failed")

            task.status = TaskStatus.FAILED
            return TaskResult(task, attempts, implementation, review, test_report)
        finally:
            async with self._integration_lock:
                self.git.worktree_remove(wt_path)
                self.git.delete_branch(task_branch)

    async def _merge_task(
        self, task: Task, arena_git: GitRepo, task_branch: str
    ) -> Tuple[bool, Optional[Review]]:
        """Squash-merge a green task into the delivery branch, gate, and accept.

        The merged state is re-verified: two tasks that each pass alone can
        still conflict, and that must surface here, not in production. On
        failure the merge is discarded and the engineer gets the gate output.
        """

        async with self._integration_lock:
            paths = [
                p
                for p in arena_git.changed_files()
                if not p.startswith(_INTERNAL_PREFIX)
            ]
            arena_git.add_paths(paths)
            arena_git.commit(f"wip(dev-team): {task.id} attempt", allow_empty=True)
            try:
                self.git.merge_squash(task_branch)
            except GitError as exc:
                # A conflicted squash-merge leaves unmerged index entries that
                # block every later merge (and could ship conflict markers in
                # the feature commit). Clean up and hand it to the engineer.
                self.git.discard_changes()
                task.status = TaskStatus.CHANGES_REQUESTED
                return False, Review(
                    approved=False,
                    summary=(
                        f"integration failed: {task.id} does not merge cleanly "
                        "onto the delivery branch"
                    ),
                    comments=[
                        ReviewComment(
                            severity=Severity.MAJOR,
                            message=(
                                "Rework the change against the current state of "
                                f"the delivery branch: {exc}"
                            ),
                        )
                    ],
                )
            dod = await asyncio.to_thread(
                self.definition_of_done.evaluate, self._gate_context(task)
            )
            if dod.passed or self._inherited_failures_only(dod):
                self.git.commit(f"wip(dev-team): {task.id}", allow_empty=True)
                return True, None
            self.git.discard_changes()
            task.status = TaskStatus.CHANGES_REQUESTED
            return False, _review_from_dod(dod)

    async def _integrate(
        self,
        task: Task,
        implementation: Implementation,
        span,
        *,
        workspace: Optional[Workspace] = None,
        git: Optional[GitRepo] = None,
        cwd: Optional[str] = None,
    ) -> Tuple[bool, Optional[Review], Optional[TestReport], Optional[Review]]:
        """Apply, review, test, and accept (or roll back) one attempt.

        Returns ``(done, review, test_report, feedback)``. By default it works
        against the engine's own workspace under the integration lock; in
        worktree mode the caller passes a per-task arena (workspace/git/cwd),
        and no lock is needed because the arena is task-private.
        """

        ws = workspace if workspace is not None else self.workspace
        repo = git if git is not None else self.git
        applier = ChangeApplier(ws)
        snapshot: Optional[_Snapshot] = None
        diff: Optional[str] = None
        try:
            if self.agentic:
                # The diff, not the engineer's self-report, defines the change:
                # any touched file the engineer forgot to list still gets
                # reviewed (and none of it can sneak into the commit unseen).
                self._merge_unreported_changes(implementation, repo)
                diff = repo.diff()
            else:
                snapshot = self._snapshot(implementation)
                applier.apply(implementation)
            contents = self._contents(implementation, ws)
            self.blackboard.post_artifact("implementation", task.id, implementation.summary)

            static_findings = await self._static_findings(cwd)
            task.status = TaskStatus.IN_REVIEW
            review = await self.reviewer.review(
                task,
                implementation,
                file_contents=contents,
                diff=diff,
                static_findings=static_findings,
                conventions=self._conventions,
                workspace_root=cwd if cwd is not None else self.workdir,
            )
            if not review.approved:
                self._scorecard["review_rejections"] = (
                    self._scorecard.get("review_rejections", 0) + 1
                )
                self._rollback(snapshot, repo)
                task.status = TaskStatus.CHANGES_REQUESTED
                self.tracer.end(span, "changes-requested")
                return False, review, None, review

            if self.config.qa_tests:
                suite = await self.qa.author_tests(
                    task,
                    implementation,
                    file_contents=contents,
                    workspace_root=cwd if cwd is not None else self.workdir,
                )
                if snapshot is not None:
                    for change in suite.files:
                        if change.path and change.path not in snapshot:
                            snapshot[change.path] = (
                                ws.read_text(change.path)
                                if ws.exists(change.path)
                                else None
                            )
                applier.apply(suite)
                self.blackboard.post_artifact("tests", task.id, suite.summary)

            task.status = TaskStatus.TESTING
            dod = await asyncio.to_thread(
                self.definition_of_done.evaluate, self._gate_context(task, cwd=cwd)
            )
            test_report = _dod_to_test_report(dod)
            if not dod.passed:
                if self._inherited_failures_only(dod):
                    self._event(
                        "gates",
                        f"{task.id}: all failing tests pre-date this delivery; accepting",
                    )
                    test_report = TestReport(
                        passed=True,
                        coverage=0.0,
                        summary=f"{dod.summary()} (all failures are pre-existing "
                        "baseline failures)",
                    )
                else:
                    self._scorecard["gate_failures"] = (
                        self._scorecard.get("gate_failures", 0) + 1
                    )
                    self._rollback(snapshot, repo)
                    task.status = TaskStatus.CHANGES_REQUESTED
                    self.tracer.end(span, "gates-failed")
                    return False, review, test_report, _review_from_dod(dod)

            try:
                vacuous = await self._tests_are_vacuous(
                    implementation, ws, repo, cwd, snapshot
                )
            except _StashRestoreFailed:
                # The fail-to-pass check could not restore the shelved change.
                # Treat it as a gate failure: roll back and reject rather than
                # accept a task whose implementation is no longer on disk.
                self._scorecard["gate_failures"] = (
                    self._scorecard.get("gate_failures", 0) + 1
                )
                self._rollback(snapshot, repo)
                task.status = TaskStatus.CHANGES_REQUESTED
                self.tracer.end(span, "stash-restore-failed")
                feedback = Review(
                    approved=False,
                    summary="The implementation could not be verified: restoring "
                    "it after the fail-to-pass check failed (stash pop conflict).",
                    comments=[
                        ReviewComment(
                            severity=Severity.MAJOR,
                            message="Re-run the task; the working tree could not be "
                            "restored after shelving the change for the "
                            "fail-to-pass check.",
                        )
                    ],
                )
                test_report = TestReport(
                    passed=False,
                    coverage=0.0,
                    summary="rejected: the shelved implementation could not be restored",
                )
                return False, review, test_report, feedback
            if vacuous:
                self._scorecard["vacuous_test_rejections"] = (
                    self._scorecard.get("vacuous_test_rejections", 0) + 1
                )
                self._rollback(snapshot, repo)
                task.status = TaskStatus.CHANGES_REQUESTED
                self.tracer.end(span, "vacuous-tests")
                feedback = Review(
                    approved=False,
                    summary="The test suite still passes with the implementation "
                    "reverted — the tests never exercise this change.",
                    comments=[
                        ReviewComment(
                            severity=Severity.MAJOR,
                            message="Write tests that fail on the pre-change code: "
                            "assert on the new behaviour's concrete inputs and "
                            "outputs, not merely that code imports or runs.",
                        )
                    ],
                )
                test_report = TestReport(
                    passed=False,
                    coverage=0.0,
                    summary="rejected: tests pass even without the implementation",
                )
                return False, review, test_report, feedback

            return True, review, test_report, None
        except Exception:
            # An attempt that dies mid-integration (budget, agent error) must
            # not leave unreviewed changes in the workspace.
            self._rollback(snapshot, repo)
            raise

    async def _static_findings(self, cwd: Optional[str]) -> Optional[str]:
        """Run the configured linter and return its output for review triage."""

        if self.config.lint_command is None:
            return None
        result = await asyncio.to_thread(
            self.command_runner.run,
            list(self.config.lint_command),
            cwd=cwd if cwd is not None else self.workdir,
            timeout=self.config.gate_timeout_seconds,
        )
        return result.output or None

    async def _tests_are_vacuous(
        self,
        implementation: Implementation,
        ws: Workspace,
        repo: GitRepo,
        cwd: Optional[str],
        snapshot: Optional[_Snapshot],
    ) -> bool:
        """Whether the gates still pass with the implementation reverted.

        A suite that passes without the change never tested it (SWT-bench's
        fail-to-pass principle). The check reverts only the implementation's
        files — QA's test files stay — reruns the gates, and restores the
        change afterwards. Skipped for dry runs, when disabled, or when there
        is nothing on disk to revert.
        """

        if not self.config.fail_to_pass_check or not self.config.qa_tests:
            return False
        if not self._local_verification:
            # Remote or degraded gates don't run the tests here; re-evaluating
            # them against reverted code cannot tell vacuous from real.
            return False
        if isinstance(self.command_runner.inner, DryRunCommandRunner):
            return False
        impl_paths = [
            c.path for c in implementation.files if c.path and ws.exists(c.path)
        ]
        if not impl_paths:
            return False

        if self.agentic:
            # The stash stack is shared by every worktree of the repo, so the
            # push/pop pair is serialised across concurrent tasks.
            async with self._stash_lock:
                if not repo.stash_push(impl_paths):
                    self._event(
                        "fail-to-pass",
                        "Fail-to-pass check skipped: the implementation could "
                        "not be shelved (stash denied or failed)",
                    )
                    return False
                try:
                    dod = await asyncio.to_thread(
                        self.definition_of_done.evaluate, self._gate_context(cwd=cwd)
                    )
                finally:
                    popped = repo.stash_pop()
            # The pop result is checked outside the lock (it does no git work):
            # a failed/conflicting pop means the implementation is no longer
            # intact on disk (still shelved, or the tree holds conflict
            # markers). Accepting now would mark the task DONE over a broken
            # tree while silently discarding the work, so abort — the call site
            # turns this into a gate failure.
            if not popped:
                self._event(
                    "fail-to-pass",
                    "Restoring the shelved implementation failed (stash pop "
                    "conflict); rejecting the attempt rather than banking a "
                    "task whose code was not restored",
                )
                raise _StashRestoreFailed
        else:
            # impl_paths were filtered on existence, so every current read works.
            current = {p: ws.read_text(p) for p in impl_paths}
            reverted = snapshot or {}
            for path in impl_paths:
                prior = reverted.get(path)
                if prior is None:
                    ws.delete(path)
                else:
                    ws.write_text(path, prior)
            try:
                dod = await asyncio.to_thread(
                    self.definition_of_done.evaluate, self._gate_context(cwd=cwd)
                )
            finally:
                for path, content in current.items():
                    ws.write_text(path, content)
        return dod.passed or self._inherited_failures_only(dod)

    def _inherited_failures_only(self, dod: DoDReport) -> bool:
        """Whether every failing test in ``dod`` was already failing at baseline.

        Requires attribution on both sides: an unparseable output can never be
        claimed as inherited.
        """

        if self._baseline_failures is None:
            return False
        current = parse_failed_tests("\n".join(g.detail for g in dod.failed_gates))
        fresh = new_failures(current, self._baseline_failures)
        return fresh is not None and not fresh

    def _merge_unreported_changes(
        self, implementation: Implementation, git: GitRepo
    ) -> None:
        """Append files git saw change that the engineer did not report."""

        reported = {c.path for c in implementation.files}
        for path in git.changed_files():
            if path.startswith(_INTERNAL_PREFIX) or path in reported:
                continue
            implementation.files.append(
                FileChange(
                    path=path,
                    change_type=ChangeType.MODIFY,
                    summary="(change detected via git, not reported by the engineer)",
                )
            )

    def _snapshot(self, implementation: Implementation) -> _Snapshot:
        """Record the pre-apply state of every path the attempt touches."""

        snapshot: _Snapshot = {}
        for change in implementation.files:
            if change.path:
                snapshot[change.path] = (
                    self.workspace.read_text(change.path)
                    if self.workspace.exists(change.path)
                    else None
                )
        return snapshot

    def _rollback(self, snapshot: Optional[_Snapshot], git: Optional[GitRepo] = None) -> None:
        """Undo a failed attempt so only gated work remains in the workspace."""

        if self.agentic:
            (git if git is not None else self.git).discard_changes()
            return
        for path, content in (snapshot or {}).items():
            if content is None:
                self.workspace.delete(path)
            else:
                self.workspace.write_text(path, content)

    def _contents(
        self, implementation: Implementation, workspace: Optional[Workspace] = None
    ) -> Dict[str, str]:
        """Read the current workspace content of the attempt's files."""

        ws = workspace if workspace is not None else self.workspace
        contents: Dict[str, str] = {}
        for change in implementation.files:
            if change.path and ws.exists(change.path):
                contents[change.path] = ws.read_text(change.path)
        return contents

    def _record_progress(self, task: Task) -> None:
        """Persist task completion to the checkpoint for crash-safe resume."""

        if self._checkpoint is None or self.checkpoints is None:
            return
        self._checkpoint.mark_done(task.id, task_fingerprint(task.title, task.description))
        self.checkpoints.save(self._checkpoint)

    def _commit_wip(self, task: Task) -> None:
        """Bank an accepted task as a WIP commit on the delivery branch.

        Rollback is a hard reset to HEAD: without banking each accepted task,
        a later task's failed attempt would wipe earlier tasks' gated work,
        and a crashed or over-budget run would leave nothing (and a dirty
        tree) for a resume to build on. The WIP commits collapse into the one
        curated feature commit at the end. Only active when a git baseline
        was prepared.
        """

        if self._baseline_sha is None:
            return
        paths = [
            p for p in self.git.changed_files() if not p.startswith(_INTERNAL_PREFIX)
        ]
        self.git.add_paths(paths)
        self.git.commit(f"wip(dev-team): {task.id}", allow_empty=True)

    def _dedupe_task_ids(self, tasks: List[Task]) -> None:
        """Rename duplicate task ids so scheduling stays unambiguous.

        The scheduler keys status by id; duplicates would abort the whole run
        after the planning spend. Dependencies keep pointing at the first
        occurrence of the original id.
        """

        seen = set()
        for task in tasks:
            if task.id in seen:
                base, n = task.id, 2
                while f"{base}-{n}" in seen:
                    n += 1
                renamed = f"{base}-{n}"
                self._event(
                    "plan-lint", f"Renamed duplicate task id {base} to {renamed}"
                )
                task.id = renamed
            seen.add(task.id)

    # -- post-task stages ----------------------------------------------------

    def _aggregate_implementation(
        self, request: FeatureRequest, task_results: List[TaskResult]
    ) -> Implementation:
        """The whole feature's change set as one implementation.

        Checkpoint-resumed tasks carry no in-memory implementation, so the
        aggregate is reconciled against git: every product path changed since
        the delivery baseline is included, whichever run changed it. Without
        this, resumed work would be committed *unseen* by the security review.
        """

        files = [
            change
            for tr in task_results
            if tr.implementation is not None
            for change in tr.implementation.files
        ]
        if self._baseline_sha is not None:
            known = {c.path for c in files}
            for path in self._delivery_changed_paths():
                if path not in known:
                    files.append(
                        FileChange(
                            path=path,
                            change_type=ChangeType.MODIFY,
                            summary="(change detected via git, e.g. from a resumed run)",
                        )
                    )
        return Implementation(
            task_id="FEATURE",
            summary=f"All changes for {request.title}",
            files=files,
        )

    def _delivery_changed_paths(self) -> List[str]:
        """Every product path changed since the delivery baseline.

        Committed (WIP commits) and uncommitted changes both count.
        """

        paths = set(self.git.diff_names(self._baseline_sha)) | set(
            self.git.changed_files()
        )
        return sorted(p for p in paths if not p.startswith(_INTERNAL_PREFIX))

    async def _security_review(
        self,
        request: FeatureRequest,
        task_results: List[TaskResult],
    ) -> SecurityReport:
        aggregate = self._aggregate_implementation(request, task_results)
        pseudo_task = Task(id="FEATURE", title=request.title, description=request.description)
        scan_command = self.config.security_scan_command or (
            self._profile.security_scan_command if self._profile else None
        )
        scanner_output = None
        scanner_failed = False
        scanner_error = None
        if scan_command is not None:
            result = await asyncio.to_thread(
                self.command_runner.run,
                list(scan_command),
                cwd=self.workdir,
                timeout=self.config.gate_timeout_seconds,
            )
            if result.exit_code in (EXIT_NOT_FOUND, EXIT_TIMEOUT):
                # The scanner never actually ran (binary missing / timed
                # out) — its exception text must not reach the agent's
                # <scanner-output> triage block looking like real findings.
                scanner_failed = True
                scanner_error = result.output or None
            else:
                scanner_output = result.output or None
        report = await self.security.review(
            pseudo_task,
            aggregate,
            file_contents=self._contents(aggregate),
            scanner_output=scanner_output,
            workspace_root=self.workdir,
        )
        report.scanner_failed = scanner_failed
        report.scanner_error = scanner_error
        self.blackboard.post_artifact("security", "FEATURE", report.summary)
        return report

    async def _provision_deployment(
        self, request: FeatureRequest, design: Design
    ) -> DeploymentPlan:
        """Get the deployment plan plus real artifacts, and materialise them."""

        listing = [
            f for f in self.workspace.list_files() if not f.startswith(_INTERNAL_PREFIX)
        ]
        plan, artifacts = await self.devops.plan_and_provision(
            request,
            design,
            workspace_listing=listing,
            project_kind=self._profile.kind if self._profile else None,
        )
        if artifacts.files:
            ChangeApplier(self.workspace).apply(artifacts)
            self.blackboard.post_artifact(
                "deployment-artifacts",
                "FEATURE",
                ", ".join(c.path for c in artifacts.files if c.path),
            )
        return plan

    async def _assess_reliability(
        self,
        request: FeatureRequest,
        design: Design,
        task_results: List[TaskResult],
        deployment: Optional[DeploymentPlan],
    ) -> ReliabilityReport:
        """Production-readiness review over the delivered evidence."""

        aggregate = self._aggregate_implementation(request, task_results)
        done = sum(1 for tr in task_results if tr.task.status is TaskStatus.DONE)
        gate_summary = f"{done}/{len(task_results)} task(s) passed their quality gates"
        return await self.sre.assess(
            request,
            design,
            aggregate,
            file_contents=self._contents(aggregate),
            deployment=deployment,
            gate_summary=gate_summary,
            workspace_root=self.workdir,
        )

    async def _write_documentation(
        self,
        request: FeatureRequest,
        design: Design,
        task_results: List[TaskResult],
    ) -> Documentation:
        """Write docs grounded in the delivered code, into the workspace."""

        aggregate = self._aggregate_implementation(request, task_results)
        existing_docs = [
            f
            for f in self.workspace.list_files()
            if f.endswith((".md", ".rst")) and not f.startswith(_INTERNAL_PREFIX)
        ]
        documentation, doc_files = await self.writer.write_docs(
            request,
            design,
            aggregate,
            file_contents=self._contents(aggregate),
            existing_docs=existing_docs,
        )
        # Captured before ChangeApplier below writes doc_files into the
        # workspace, so a new doc's citation of another new doc it
        # introduces is correctly evaluated against pre-existing state.
        known_files = self.workspace.list_files()
        documentation.unverified_claims = doc_claim_issues(doc_files.files, known_files)
        if doc_files.files:
            ChangeApplier(self.workspace).apply(doc_files)
            self.blackboard.post_artifact(
                "documentation-files",
                "FEATURE",
                ", ".join(c.path for c in doc_files.files if c.path),
            )
        return documentation

    def _commit_if_approved(
        self,
        request: FeatureRequest,
        task_results: List[TaskResult],
        security: Optional[SecurityReport],
    ) -> bool:
        """Commit gated work once, and only when security did not block."""

        done = [tr.task.id for tr in task_results if tr.task.status is TaskStatus.DONE]
        if not self._can_commit or not done:
            return False
        if security is None or not security.approved:
            # No security verdict (e.g. budget died first) is treated the same
            # as a block: nothing unvetted gets committed.
            self._event("commit", "Skipping commit: no security approval for the release")
            return False
        decision = self.approval.review(
            ApprovalRequest(
                action=f"commit feature: {request.title}",
                detail=f"{len(done)} task(s): {', '.join(done)}",
                risk="medium",
            )
        )
        if not decision.approved:
            self._event("commit", "Skipping commit: approval denied", detail=decision.reason)
            return False
        # Captured before any reset so the except branch can undo a soft reset
        # whose follow-up commit failed (see below). ``None`` while no reset
        # has moved the tip, so the else branch never triggers a restore.
        head: Optional[str] = None
        try:
            if self._baseline_sha is not None:
                # Accepted tasks already live as WIP commits on the delivery
                # branch; collapse them into the single feature commit, and
                # pick up post-task artifacts (docs, deployment files) that
                # were written after the last accepted task.
                head = self.git.rev_parse("HEAD")
                if head == self._baseline_sha:
                    return False
                self.git.reset_soft(self._baseline_sha)
                extras = [
                    p
                    for p in self.git.changed_files()
                    if not p.startswith(_INTERNAL_PREFIX)
                ]
                self.git.add_paths(extras)
                self.git.commit(f"{request.title} ({', '.join(done)})")
            else:
                self.git.ensure_repo()
                # Stage a curated change set, never `add -A`: the engine's own
                # bookkeeping must not ship in the feature commit.
                paths = [
                    p
                    for p in self.git.changed_files()
                    if not p.startswith(_INTERNAL_PREFIX)
                ]
                if not paths:
                    return False
                self.git.add_paths(paths)
                self.git.commit(f"{request.title} ({', '.join(done)})")
        except GitError as exc:
            self._event("commit", "Commit failed", detail=str(exc))
            # The soft reset already moved the tip back to the baseline, so the
            # banked WIP commits now live only in the reflog. A failed final
            # commit would strand them there and brick resume. Restore the tip
            # to the pre-reset HEAD so those commits (and a resumable state)
            # return before we bail out. ``head`` is None only in the no-baseline
            # branch, where nothing moved the tip and there is nothing to undo.
            if head is not None:
                self.git.reset_hard(head)
            return False
        self._event("commit", f"Committed {len(done)} task(s)")
        return True

    def _register_backlog(self, request: FeatureRequest, tasks: List[Task]):
        """Mirror the plan into the persistent backlog, if one is configured.

        Reruns and resumes of the same feature update the existing epic and
        stories instead of minting duplicates on every run.
        """

        if self.backlog_store is None:
            return None, {}
        backlog = self.backlog_store.load()
        epic = next((e for e in backlog.epics if e.title == request.title), None)
        if epic is None:
            epic = backlog.add_epic(request.title, request.description)
        existing = {s.title: s for s in backlog.stories_for_epic(epic.id)}
        stories = {}
        for task in tasks:
            story = existing.get(task.title)
            if story is None:
                story = backlog.add_story(task.title, task.description, epic_id=epic.id)
            story.status = ItemStatus.IN_PROGRESS
            stories[task.id] = story
        self.backlog_store.save(backlog)
        return backlog, stories

    def _finalise_backlog(self, backlog, stories, task_results: List[TaskResult]) -> None:
        if self.backlog_store is None or backlog is None:
            return
        for tr in task_results:
            story = stories.get(tr.task.id)
            if story is not None:
                story.status = (
                    ItemStatus.DONE
                    if tr.task.status is TaskStatus.DONE
                    else ItemStatus.BLOCKED
                )
        self.backlog_store.save(backlog)
