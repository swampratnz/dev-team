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
- **Nothing is committed until security approves.** Task work lands in the
  workspace as it passes gates; the git commit happens once, at the end, and
  only when the security review did not block.
- **A blown budget stops the run, not the world.** Budget exhaustion fails
  remaining work gracefully and still returns a full (partial) outcome with
  trace, cost, and checkpoint intact — a later run resumes from the checkpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

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
from .approval import ApprovalGate, AutoApprover
from .backlog import BacklogStore, ItemStatus
from .budget import Budget, BudgetExceededError
from .changes import ChangeApplier
from .context import build_repo_context
from .events import AgentEvent, Listener, emit
from .failures import new_failures, parse_failed_tests
from .execution import (
    CommandRunner,
    DryRunCommandRunner,
    InMemoryWorkspace,
    LocalWorkspace,
    SubprocessCommandRunner,
    Workspace,
)
from .git import GitError, GitRepo
from .instrument import InstrumentedRunner
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
from .policy import GuardedCommandRunner, SideEffectPolicy
from .profile import detect_project
from .scheduler import ScheduledResult, schedule
from .sdk import AgentRunner
from .trace import Tracer
from .verification import CommandGate, DefinitionOfDone, GateContext, DoDReport

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
    worktrees: bool = False

    def __post_init__(self) -> None:
        if self.max_task_attempts < 1:
            raise ValueError("max_task_attempts must be at least 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.json_retries < 0:
            raise ValueError("json_retries must be non-negative")


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
        if self.security is not None and not self.security.approved:
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
"""


def _branch_slug(title: str) -> str:
    """Derive a git-safe branch segment from a feature title."""

    cleaned = "".join(c.lower() if c.isalnum() else "-" for c in title)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:40] or "feature"


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
    ) -> None:
        self.config = config or EngineConfig()
        self.workspace: Workspace = workspace or InMemoryWorkspace()
        self.budget = budget or Budget()
        self.tracer = tracer or Tracer()
        self.blackboard = blackboard or Blackboard()
        self.approval = approval or AutoApprover()
        self.listener = listener

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
        # Gate resolution: an injected DoD wins; an explicit verify_command
        # builds one; otherwise the command is auto-detected from the
        # workspace at deliver time (after setup, when manifests exist).
        if definition_of_done is not None:
            self.definition_of_done: Optional[DefinitionOfDone] = definition_of_done
        elif self.config.verify_command is not None:
            self.definition_of_done = DefinitionOfDone(
                [CommandGate("tests", self.config.verify_command)]
            )
        else:
            self.definition_of_done = None
        self.backlog_store = backlog_store
        self.memory = memory or ProjectMemory(self.workspace)
        self.checkpoints = checkpoints or (
            CheckpointStore(self.workspace) if self.config.resume else None
        )
        self._integration_lock = asyncio.Lock()
        self._checkpoint: Optional[RunCheckpoint] = None
        self._budget_exhausted = False
        self._branch: Optional[str] = None
        self._baseline_failures = None
        self._baseline_sha: Optional[str] = None

        def make(cls):
            wrapped = InstrumentedRunner(
                runner, cls.role, budget=self.budget, tracer=self.tracer
            )
            return cls(
                wrapped,
                model=self.config.role_models.get(cls.role, self.config.model),
                listener=listener,
                json_retries=self.config.json_retries,
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

        if self.agentic:
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

        self._resolve_gates()

        self._baseline_failures = None
        baseline = self._check_baseline()
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
        context_parts = [
            part
            for part in (_prior_context(self.memory.load()), repo_ctx.render() or None)
            if part
        ]
        prior = "\n\n".join(context_parts) if context_parts else None
        plan = await self.manager.create_plan(request, prior_context=prior)
        self.blackboard.post_artifact("plan", "plan", plan.summary)
        self._event("planned", "Plan ready", detail=f"{len(plan.tasks)} task(s)")

        design = await self.architect.design(
            request, plan, repo_context=repo_ctx.render() or None
        )
        self.blackboard.record_decision(
            title=f"Architecture for {request.title}",
            context=request.description,
            decision=design.overview,
        )
        self._event("designed", "Design ready")

        backlog, stories = self._register_backlog(request, plan.tasks)

        self._checkpoint = (
            self.checkpoints.load(request.title) if self.checkpoints is not None else None
        )
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

        await schedule(
            pending,
            worker,
            max_concurrency=self.config.max_concurrency,
            listener=self._on_scheduled,
        )

        task_results: List[TaskResult] = []
        for task in plan.tasks:
            if task.id in results:
                task_results.append(results[task.id])
            else:  # cascade-skipped by the scheduler
                task.status = TaskStatus.FAILED
                task_results.append(TaskResult(task=task, attempts=0))

        security = await self._specialist(self._security_review(request, task_results))
        documentation = await self._specialist(self.writer.document(request, design))
        reliability = await self._specialist(self.sre.assess(request, design))
        deployment = await self._specialist(self.devops.plan_deployment(request, design))

        committed = self._commit_if_approved(request, task_results, security)
        self._finalise_backlog(backlog, stories, task_results)
        notes = _retrospective(task_results, security)
        if notes:
            self.blackboard.put("retrospective", notes)
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
        )
        if outcome.success and self.checkpoints is not None:
            self.checkpoints.clear()
        verdict = "succeeded" if outcome.success else "with issues"
        self._event("done", f"Delivery finished {verdict}", detail=f"${outcome.cost_usd:.4f}")
        return outcome

    # -- run preparation -------------------------------------------------------

    def _prepare_git_baseline(self, request: FeatureRequest) -> Optional[DeliveryOutcome]:
        """Get the repo ready for agentic work; return a halted outcome if unsafe.

        Ensures a repo exists, refuses to run over uncommitted work (unless
        explicitly allowed), authors a minimal .gitignore on repos that lack
        one, switches to a dedicated delivery branch, and commits the baseline
        that failed attempts roll back to.
        """

        self.git.ensure_repo()
        if self.git.has_changes() and not self.config.allow_dirty_baseline:
            return self._halted(
                request,
                "the working tree has uncommitted changes; commit or stash them "
                "first, or set allow_dirty_baseline=True to sweep them into a "
                "baseline commit on the delivery branch",
            )
        if self.config.write_gitignore and not self.workspace.exists(".gitignore"):
            self.workspace.write_text(".gitignore", _DEFAULT_GITIGNORE)
        if self.config.use_branch:
            self._branch = self.config.branch or f"dev-team/{_branch_slug(request.title)}"
            self.git.switch_to(self._branch)
        if self.git.has_changes():
            self.git.add_all()
            self.git.commit("chore(dev-team): baseline before delivery")
        if self._use_worktrees:
            # Worktrees branch from HEAD, so one must exist; and the final
            # squash needs the exact sha the delivery started from.
            if not self.git.has_commits():
                self.git.commit("chore(dev-team): init", allow_empty=True)
            self._baseline_sha = self.git.rev_parse("HEAD")
        return None

    def _resolve_gates(self) -> None:
        """Build the Definition of Done from the workspace when not configured."""

        if self.definition_of_done is not None:
            return
        profile = detect_project(self.workspace)
        self.definition_of_done = DefinitionOfDone(
            [CommandGate("tests", profile.verify_command)]
        )
        self.blackboard.put("project_profile", profile.kind)
        self._event(
            "gates",
            f"Auto-detected {profile.kind} project",
            detail=f"verify: {' '.join(profile.verify_command)} ({profile.reason})",
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

    def _check_baseline(self) -> Optional[DoDReport]:
        """Evaluate the gates before any work starts, when there is anything
        to evaluate.

        An empty workspace (greenfield) has no baseline to check — and a
        ``.gitignore`` the engine itself just authored doesn't make it any
        less empty. On a populated workspace this is what separates inherited
        breakage from breakage the team introduces — without it, a legacy
        repo's one flaky test fails every task and the engineer gets blamed
        for code it never touched.
        """

        if not [f for f in self.workspace.list_files() if f != ".gitignore"]:
            return None
        report = self.definition_of_done.evaluate(self._gate_context())
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
        """Run a post-task specialist stage, degrading gracefully on budget."""

        try:
            return await coro
        except BudgetExceededError:
            self._budget_exhausted = True
            self._event("budget", "Budget exhausted; skipping remaining specialist stages")
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

    async def _develop_task(self, task: Task, design: Design) -> TaskResult:
        if self._use_worktrees:
            return await self._develop_task_in_worktree(task, design)

        feedback: Optional[Review] = None
        implementation: Optional[Implementation] = None
        review: Optional[Review] = None
        test_report: Optional[TestReport] = None
        attempts = 0

        while attempts < self.config.max_task_attempts:
            attempts += 1
            model = self._attempt_model(attempts)
            task.status = TaskStatus.IN_PROGRESS
            span = self.tracer.start("task", task.id, attempt=str(attempts))

            if self.agentic:
                # The agentic engineer mutates the shared working directory, so
                # the whole attempt runs inside the integration lock.
                async with self._integration_lock:
                    implementation = await self.engineer.implement_in_place(
                        task,
                        design,
                        feedback,
                        cwd=str(self.workdir),
                        model=model,
                        tools=self.config.engineer_tools,
                    )
                    done, review, test_report, feedback = await self._integrate(
                        task, implementation, span
                    )
            else:
                implementation = await self.engineer.implement(
                    task,
                    design,
                    feedback,
                    workspace_listing=[
                        f
                        for f in self.workspace.list_files()
                        if not f.startswith(_INTERNAL_PREFIX)
                    ],
                    model=model,
                )
                async with self._integration_lock:
                    done, review, test_report, feedback = await self._integrate(
                        task, implementation, span
                    )

            if done:
                task.status = TaskStatus.DONE
                self._record_progress(task)
                self.tracer.end(span, "done")
                return TaskResult(task, attempts, implementation, review, test_report)

        task.status = TaskStatus.FAILED
        return TaskResult(task, attempts, implementation, review, test_report)

    async def _develop_task_in_worktree(self, task: Task, design: Design) -> TaskResult:
        """Develop ``task`` in its own git worktree, merging only when green.

        Implementation, review, and gate runs all happen inside the task's
        worktree — in parallel with other tasks. Only the squash-merge into
        the delivery branch (plus a full gate check on the merged state) is
        serialised.
        """

        wt_path = f"{self.workdir}/.dev_team/worktrees/{task.id.lower()}"
        task_branch = f"{self._branch or 'dev-team'}-task-{task.id.lower()}"
        async with self._integration_lock:  # worktree creation mutates .git
            self.git.worktree_add(wt_path, task_branch)
        arena_ws = LocalWorkspace(wt_path)
        arena_git = GitRepo(self.command_runner, cwd=wt_path)

        feedback: Optional[Review] = None
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
            self.git.merge_squash(task_branch)
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

            task.status = TaskStatus.IN_REVIEW
            review = await self.reviewer.review(
                task, implementation, file_contents=contents, diff=diff
            )
            if not review.approved:
                self._rollback(snapshot, repo)
                task.status = TaskStatus.CHANGES_REQUESTED
                self.tracer.end(span, "changes-requested")
                return False, review, None, review

            if self.config.qa_tests:
                suite = await self.qa.author_tests(
                    task, implementation, file_contents=contents
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
                    self._rollback(snapshot, repo)
                    task.status = TaskStatus.CHANGES_REQUESTED
                    self.tracer.end(span, "gates-failed")
                    return False, review, test_report, _review_from_dod(dod)

            return True, review, test_report, None
        except Exception:
            # An attempt that dies mid-integration (budget, agent error) must
            # not leave unreviewed changes in the workspace.
            self._rollback(snapshot, repo)
            raise

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

    # -- post-task stages ----------------------------------------------------

    async def _security_review(
        self,
        request: FeatureRequest,
        task_results: List[TaskResult],
    ) -> SecurityReport:
        files = [
            change
            for tr in task_results
            if tr.implementation is not None
            for change in tr.implementation.files
        ]
        aggregate = Implementation(
            task_id="FEATURE",
            summary=f"All changes for {request.title}",
            files=files,
        )
        pseudo_task = Task(id="FEATURE", title=request.title, description=request.description)
        report = await self.security.review(
            pseudo_task, aggregate, file_contents=self._contents(aggregate)
        )
        self.blackboard.post_artifact("security", "FEATURE", report.summary)
        return report

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
        try:
            if self._use_worktrees:
                # Accepted tasks already live as WIP commits on the delivery
                # branch; collapse them into the single feature commit.
                head = self.git.rev_parse("HEAD")
                if not self._baseline_sha or head == self._baseline_sha:
                    return False
                self.git.reset_soft(self._baseline_sha)
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
            return False
        self._event("commit", f"Committed {len(done)} task(s)")
        return True

    def _register_backlog(self, request: FeatureRequest, tasks: List[Task]):
        """Mirror the plan into the persistent backlog, if one is configured."""

        if self.backlog_store is None:
            return None, {}
        backlog = self.backlog_store.load()
        epic = backlog.add_epic(request.title, request.description)
        stories = {
            task.id: backlog.add_story(task.title, task.description, epic_id=epic.id)
            for task in tasks
        }
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
