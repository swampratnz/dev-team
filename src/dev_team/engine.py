"""The delivery engine: real, gated, observable feature delivery.

Where :class:`~dev_team.workflow.DevelopmentWorkflow` *simulates* a run, the
:class:`DeliveryEngine` actually does the work: it materialises the engineer's
changes into a :class:`~dev_team.execution.Workspace`, runs executable quality
gates via a :class:`~dev_team.execution.CommandRunner`, schedules independent
tasks concurrently, commits through git, and threads budget, tracing, shared
memory, and specialist review (security/docs/SRE) through the whole thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .agents import (
    ArchitectAgent,
    DevOpsAgent,
    EngineerAgent,
    ProductManagerAgent,
    ReviewerAgent,
    SecurityEngineerAgent,
    SREAgent,
    TechnicalWriterAgent,
)
from .approval import ApprovalGate, AutoApprover
from .budget import Budget
from .changes import ChangeApplier
from .events import AgentEvent, Listener, emit
from .execution import (
    CommandRunner,
    InMemoryWorkspace,
    SubprocessCommandRunner,
    Workspace,
)
from .git import GitRepo
from .instrument import InstrumentedRunner
from .memory import Blackboard
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


@dataclass
class EngineConfig:
    """Settings for a :class:`DeliveryEngine`."""

    model: Optional[str] = None
    max_task_attempts: int = 3
    max_concurrency: int = 4
    verify_command: Sequence[str] = ("pytest", "-q")
    commit: bool = True

    def __post_init__(self) -> None:
        if self.max_task_attempts < 1:
            raise ValueError("max_task_attempts must be at least 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")


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
        coverage=100.0 if report.passed else 0.0,
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
        listener: Optional[Listener] = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.workspace: Workspace = workspace or InMemoryWorkspace()
        self.budget = budget or Budget()
        self.tracer = tracer or Tracer()
        self.blackboard = blackboard or Blackboard()
        self.approval = approval or AutoApprover()
        self.listener = listener

        base_runner = command_runner or SubprocessCommandRunner()
        self.command_runner: CommandRunner = GuardedCommandRunner(
            base_runner,
            policy=policy or SideEffectPolicy(),
            approval=self.approval,
        )
        self.git = git if git is not None else GitRepo(self.command_runner)
        self.change_applier = ChangeApplier(self.workspace)
        self.definition_of_done = definition_of_done or DefinitionOfDone(
            [CommandGate("tests", self.config.verify_command)]
        )

        def make(cls):
            wrapped = InstrumentedRunner(
                runner, cls.role, budget=self.budget, tracer=self.tracer
            )
            return cls(wrapped, model=self.config.model, listener=listener)

        self.manager = make(ProductManagerAgent)
        self.architect = make(ArchitectAgent)
        self.engineer = make(EngineerAgent)
        self.reviewer = make(ReviewerAgent)
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

        plan = await self.manager.create_plan(request)
        self.blackboard.post_artifact("plan", "plan", plan.summary)
        self._event("planned", "Plan ready", detail=f"{len(plan.tasks)} task(s)")

        design = await self.architect.design(request, plan)
        self.blackboard.record_decision(
            title=f"Architecture for {request.title}",
            context=request.description,
            decision=design.overview,
        )
        self._event("designed", "Design ready")

        results: dict = {}

        async def worker(task: Task) -> bool:
            outcome = await self._develop_task(task, design)
            results[task.id] = outcome
            return outcome.succeeded

        await schedule(
            plan.tasks,
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

        security = await self._security_review(request, task_results)
        documentation = await self.writer.document(request, design)
        reliability = await self.sre.assess(request, design)
        deployment = await self.devops.plan_deployment(request, design)

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
            workspace_files=self.workspace.list_files(),
        )
        verdict = "succeeded" if outcome.success else "with issues"
        self._event("done", f"Delivery finished {verdict}", detail=f"${outcome.cost_usd:.4f}")
        return outcome

    def _on_scheduled(self, result: ScheduledResult) -> None:
        self._event("scheduled", f"{result.task_id} {result.status.value}")

    async def _develop_task(self, task: Task, design: Design) -> TaskResult:
        feedback: Optional[Review] = None
        implementation: Optional[Implementation] = None
        review: Optional[Review] = None
        test_report: Optional[TestReport] = None
        attempts = 0

        while attempts < self.config.max_task_attempts:
            attempts += 1
            task.status = TaskStatus.IN_PROGRESS
            span = self.tracer.start("task", task.id, attempt=str(attempts))

            implementation = await self.engineer.implement(task, design, feedback)
            self.change_applier.apply(implementation)
            self.blackboard.post_artifact("implementation", task.id, implementation.summary)

            task.status = TaskStatus.IN_REVIEW
            review = await self.reviewer.review(task, implementation)
            if not review.approved:
                task.status = TaskStatus.CHANGES_REQUESTED
                feedback = review
                self.tracer.end(span, "changes-requested")
                continue

            task.status = TaskStatus.TESTING
            dod = self.definition_of_done.evaluate(
                GateContext(runner=self.command_runner, workspace=self.workspace, task=task)
            )
            test_report = _dod_to_test_report(dod)
            if not dod.passed:
                task.status = TaskStatus.CHANGES_REQUESTED
                feedback = _review_from_dod(dod)
                self.tracer.end(span, "gates-failed")
                continue

            if self.config.commit:
                self.git.add_all()
                self.git.commit(f"{task.id}: {task.title}")
            task.status = TaskStatus.DONE
            self.tracer.end(span, "done")
            return TaskResult(task, attempts, implementation, review, test_report)

        task.status = TaskStatus.FAILED
        return TaskResult(task, attempts, implementation, review, test_report)

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
        report = await self.security.review(pseudo_task, aggregate)
        self.blackboard.post_artifact("security", "FEATURE", report.summary)
        return report
