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
from .events import AgentEvent, Listener, emit
from .execution import (
    CommandRunner,
    DryRunCommandRunner,
    InMemoryWorkspace,
    SubprocessCommandRunner,
    Workspace,
)
from .git import GitError, GitRepo
from .instrument import InstrumentedRunner
from .memory import Blackboard, CheckpointStore, ProjectMemory, RunCheckpoint
from .models import (
    Design,
    DeploymentPlan,
    Documentation,
    FeatureRequest,
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
from .scheduler import ScheduledResult, schedule
from .sdk import AgentRunner
from .trace import Tracer
from .verification import CommandGate, DefinitionOfDone, GateContext, DoDReport

# Internal bookkeeping lives under this prefix and is not part of the product.
_INTERNAL_PREFIX = ".dev_team/"


@dataclass
class EngineConfig:
    """Settings for a :class:`DeliveryEngine`."""

    model: Optional[str] = None
    max_task_attempts: int = 3
    max_concurrency: int = 4
    verify_command: Sequence[str] = ("pytest", "-q")
    commit: bool = True
    agentic: Optional[bool] = None
    qa_tests: bool = True
    json_retries: int = 1
    role_models: Mapping[str, str] = field(default_factory=dict)
    escalation_model: Optional[str] = None
    resume: bool = True

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
    if not lines:
        return None
    return "\n".join(lines)


# A snapshot maps path -> prior content, or None when the file did not exist.
_Snapshot = Dict[str, Optional[str]]


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

        self.change_applier = ChangeApplier(self.workspace)
        self.definition_of_done = definition_of_done or DefinitionOfDone(
            [CommandGate("tests", self.config.verify_command)]
        )
        self.backlog_store = backlog_store
        self.memory = memory or ProjectMemory(self.workspace)
        self.checkpoints = checkpoints or (
            CheckpointStore(self.workspace) if self.config.resume else None
        )
        self._integration_lock = asyncio.Lock()
        self._checkpoint: Optional[RunCheckpoint] = None
        self._budget_exhausted = False

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

        if self.agentic:
            # Agentic attempts are rolled back via git, so the baseline (any
            # pre-existing uncommitted work included) must be committed first.
            self.git.ensure_repo()
            if self.git.has_changes():
                self.git.add_all()
                self.git.commit("chore(dev-team): baseline before delivery")

        prior = _prior_context(self.memory.load())
        plan = await self.manager.create_plan(request, prior_context=prior)
        self.blackboard.post_artifact("plan", "plan", plan.summary)
        self._event("planned", "Plan ready", detail=f"{len(plan.tasks)} task(s)")

        design = await self.architect.design(request, plan)
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
            if self._checkpoint is not None and task.id in self._checkpoint.done_task_ids:
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
        )
        if outcome.success and self.checkpoints is not None:
            self.checkpoints.clear()
        verdict = "succeeded" if outcome.success else "with issues"
        self._event("done", f"Delivery finished {verdict}", detail=f"${outcome.cost_usd:.4f}")
        return outcome

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

    async def _develop_task(self, task: Task, design: Design) -> TaskResult:
        feedback: Optional[Review] = None
        implementation: Optional[Implementation] = None
        review: Optional[Review] = None
        test_report: Optional[TestReport] = None
        attempts = 0

        while attempts < self.config.max_task_attempts:
            attempts += 1
            model = (
                self.config.escalation_model
                if attempts == self.config.max_task_attempts
                else None
            )
            task.status = TaskStatus.IN_PROGRESS
            span = self.tracer.start("task", task.id, attempt=str(attempts))

            if self.agentic:
                # The agentic engineer mutates the shared working directory, so
                # the whole attempt runs inside the integration lock.
                async with self._integration_lock:
                    implementation = await self.engineer.implement_in_place(
                        task, design, feedback, cwd=str(self.workdir), model=model
                    )
                    done, review, test_report, feedback = await self._integrate(
                        task, implementation, span
                    )
            else:
                implementation = await self.engineer.implement(
                    task,
                    design,
                    feedback,
                    workspace_listing=self.workspace.list_files(),
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

    async def _integrate(
        self,
        task: Task,
        implementation: Implementation,
        span,
    ) -> Tuple[bool, Optional[Review], Optional[TestReport], Optional[Review]]:
        """Apply, review, test, and accept (or roll back) one attempt.

        Returns ``(done, review, test_report, feedback)``. Runs under the
        integration lock: the workspace only ever contains either gated work
        or the current attempt, never two attempts interleaved.
        """

        snapshot: Optional[_Snapshot] = None
        try:
            if not self.agentic:
                snapshot = self._snapshot(implementation)
                self.change_applier.apply(implementation)
            contents = self._contents(implementation)
            self.blackboard.post_artifact("implementation", task.id, implementation.summary)

            task.status = TaskStatus.IN_REVIEW
            review = await self.reviewer.review(task, implementation, file_contents=contents)
            if not review.approved:
                self._rollback(snapshot)
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
                                self.workspace.read_text(change.path)
                                if self.workspace.exists(change.path)
                                else None
                            )
                self.change_applier.apply(suite)
                self.blackboard.post_artifact("tests", task.id, suite.summary)

            task.status = TaskStatus.TESTING
            dod = await asyncio.to_thread(
                self.definition_of_done.evaluate,
                GateContext(
                    runner=self.command_runner,
                    workspace=self.workspace,
                    task=task,
                    cwd=self.workdir,
                ),
            )
            test_report = _dod_to_test_report(dod)
            if not dod.passed:
                self._rollback(snapshot)
                task.status = TaskStatus.CHANGES_REQUESTED
                self.tracer.end(span, "gates-failed")
                return False, review, test_report, _review_from_dod(dod)

            return True, review, test_report, None
        except Exception:
            # An attempt that dies mid-integration (budget, agent error) must
            # not leave unreviewed changes in the workspace.
            self._rollback(snapshot)
            raise

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

    def _rollback(self, snapshot: Optional[_Snapshot]) -> None:
        """Undo a failed attempt so only gated work remains in the workspace."""

        if self.agentic:
            self.git.discard_changes()
            return
        for path, content in (snapshot or {}).items():
            if content is None:
                self.workspace.delete(path)
            else:
                self.workspace.write_text(path, content)

    def _contents(self, implementation: Implementation) -> Dict[str, str]:
        """Read the current workspace content of the attempt's files."""

        contents: Dict[str, str] = {}
        for change in implementation.files:
            if change.path and self.workspace.exists(change.path):
                contents[change.path] = self.workspace.read_text(change.path)
        return contents

    def _record_progress(self, task: Task) -> None:
        """Persist task completion to the checkpoint for crash-safe resume."""

        if self._checkpoint is None or self.checkpoints is None:
            return
        self._checkpoint.done_task_ids.append(task.id)
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
            self.git.ensure_repo()
            self.git.add_all()
            if not self.git.has_changes():
                return False
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
