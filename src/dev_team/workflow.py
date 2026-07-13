"""The development workflow: a state machine coordinating the agents."""

from __future__ import annotations

from typing import List, Optional

from .agents import (
    ArchitectAgent,
    DevOpsAgent,
    EngineerAgent,
    ProductManagerAgent,
    QAAgent,
    ReviewerAgent,
)
from .budget import Budget, BudgetExceededError
from .config import TeamConfig
from .errors import WorkflowError
from .events import AgentEvent, Listener, emit
from .interaction import (
    InteractionChannel,
    ask_in_thread,
    plan_review_question,
)
from .models import (
    Design,
    DeploymentPlan,
    FeatureRequest,
    Plan,
    ProjectResult,
    Review,
    ReviewComment,
    Severity,
    Task,
    TaskResult,
    TaskStatus,
    TestReport,
)
from .ordering import topological_order


class DevelopmentWorkflow:
    """Drives a feature request through the full development lifecycle.

    The workflow: plan → design → (implement → review → test)* per task →
    deploy. Each task may be re-attempted up to ``config.max_task_attempts``
    times when review or QA rejects it.

    Every agent call is metered against :attr:`budget` (the agents are wired to
    an :class:`~dev_team.instrument.InstrumentedRunner` bound to it by
    :func:`~dev_team.team.build_workflow`). The default budget is uncapped — it
    only accumulates cost — but a caller may supply a capped :class:`Budget`,
    in which case :meth:`run` stops at the ceiling gracefully (checked before
    each agent call) rather than crashing, mirroring the delivery engine.
    """

    def __init__(
        self,
        *,
        manager: ProductManagerAgent,
        architect: ArchitectAgent,
        engineer: EngineerAgent,
        reviewer: ReviewerAgent,
        qa: QAAgent,
        devops: DevOpsAgent,
        config: Optional[TeamConfig] = None,
        listener: Optional[Listener] = None,
        interaction: Optional[InteractionChannel] = None,
        budget: Optional[Budget] = None,
    ) -> None:
        self.manager = manager
        self.architect = architect
        self.engineer = engineer
        self.reviewer = reviewer
        self.qa = qa
        self.devops = devops
        self.config = config or TeamConfig()
        self.listener = listener
        self.interaction = interaction
        # Always meter, even uncapped: cost surfacing needs a live meter, and
        # the agents record into this exact instance (see build_workflow).
        self.budget = budget if budget is not None else Budget()

    def _emit(self, stage: str, message: str, detail: Optional[str] = None) -> None:
        emit(
            self.listener,
            AgentEvent(
                role="workflow", stage=stage, message=message, detail=detail
            ),
        )

    def _tests_pass(self, report: TestReport) -> bool:
        """Whether a test report clears the QA bar."""

        return report.passed and report.coverage >= self.config.min_coverage

    @staticmethod
    def _feedback_from_tests(report: TestReport) -> Review:
        """Synthesise reviewer-style feedback from a failing test report."""

        return Review(
            approved=False,
            summary=(
                f"Tests did not pass (coverage {report.coverage:.0f}%). "
                f"{report.summary}"
            ),
            comments=[
                ReviewComment(
                    severity=Severity.MAJOR,
                    message="Fix failing tests and reach the required coverage.",
                )
            ],
        )

    async def run(self, request: FeatureRequest) -> ProjectResult:
        """Execute the full workflow for ``request``.

        A capped :attr:`budget` stops the run at its ceiling *gracefully*: the
        stage that would push over the limit raises
        :class:`~dev_team.budget.BudgetExceededError`, which is caught here so
        a populated (partial) :class:`ProjectResult` is still returned — with
        the metered ``cost_usd`` intact — instead of unwinding the caller.
        """

        self._emit("start", f"Starting development: {request.title}")

        plan: Optional[Plan] = None
        design: Optional[Design] = None
        task_results: List[TaskResult] = []
        deployment: Optional[DeploymentPlan] = None
        try:
            plan = await self.manager.create_plan(request)
            self._emit("planned", "Plan ready", detail=f"{len(plan.tasks)} task(s)")
            plan = await self._reviewed_plan(request, plan)

            design = await self.architect.design(request, plan)
            self._emit(
                "designed",
                "Design ready",
                detail=f"{len(design.components)} component(s)",
            )

            task_results = await self._develop_tasks(plan, design)

            deployment = await self.devops.plan_deployment(request, design)
            self._emit("deployment", "Deployment plan ready")
        except BudgetExceededError as exc:
            # A blown budget stops the run, not the world: whatever completed
            # before the ceiling is preserved and reported.
            self._emit(
                "budget",
                "Budget exhausted; stopping the run gracefully",
                detail=str(exc),
            )

        result = ProjectResult(
            request=request,
            plan=plan if plan is not None else Plan(summary=""),
            design=design if design is not None else Design(overview=""),
            task_results=task_results,
            deployment=deployment,
            cost_usd=self.budget.spent,
        )
        status = "successfully" if result.success else "with failures"
        self._emit(
            "done",
            f"Finished {status}",
            detail=f"{len(result.completed_tasks)}/{len(task_results)} task(s) done",
        )
        return result

    async def _develop_tasks(
        self, plan: Plan, design: Design
    ) -> List[TaskResult]:
        """Develop each task in dependency order, cascade-skipping the doomed.

        A task whose dependency did not reach ``DONE`` is skipped — marked
        FAILED with no implement/review/test agent calls — exactly as the
        delivery scheduler cascade-skips dependents of a failed task: running a
        dependent on top of missing work only burns budget on something that
        cannot succeed. Only *real* dependencies gate a task (unknown ids and
        self-edges are ignored, matching :func:`topological_order`), so a bogus
        dependency never skips a task that could have run.

        Budget exhaustion is likewise handled gracefully per task: a task that
        cannot even start (the ceiling is already reached) or whose work trips
        the ceiling mid-flight is failed without aborting the whole loop.
        """

        ordered = topological_order(plan.tasks)
        known_ids = {task.id for task in plan.tasks}
        done_ids: set[str] = set()
        results: List[TaskResult] = []
        for task in ordered:
            unmet = [
                dep
                for dep in task.dependencies
                if dep in known_ids and dep != task.id and dep not in done_ids
            ]
            if unmet:
                task.status = TaskStatus.FAILED
                self._emit(
                    "task-skipped",
                    f"{task.id} skipped",
                    detail=f"unmet dependency: {', '.join(unmet)}",
                )
                results.append(TaskResult(task=task, attempts=0))
                continue
            if self.budget.exhausted:
                task.status = TaskStatus.FAILED
                self._emit("budget", f"Budget exhausted; skipping {task.id}")
                results.append(TaskResult(task=task, attempts=0))
                continue
            try:
                result = await self._develop_task(task, design)
            except BudgetExceededError as exc:
                task.status = TaskStatus.FAILED
                self._emit(
                    "budget",
                    f"Budget exhausted during {task.id}",
                    detail=str(exc),
                )
                result = TaskResult(task=task, attempts=0)
            results.append(result)
            if result.succeeded:
                done_ids.add(task.id)
        return results

    async def _reviewed_plan(self, request: FeatureRequest, plan):
        """Present the plan for interactive review, revising until approved.

        Without an interaction channel the plan passes through untouched.
        Raises :class:`WorkflowError` when the human aborts the run.
        """

        if self.interaction is None:
            return plan
        asker = (
            self.manager.persona.name
            if self.manager.persona is not None
            else self.manager.role
        )
        while True:
            reply = await ask_in_thread(
                self.interaction, plan_review_question(plan, asked_by=asker)
            )
            if reply.choice == "approve":
                self._emit("plan-approved", "Plan approved interactively")
                return plan
            if reply.choice == "abort":
                self._emit("aborted", "Run aborted at plan review")
                raise WorkflowError("run aborted at plan review")
            self._emit(
                "plan-revision",
                "Plan revision requested",
                detail=reply.text or None,
            )
            plan = await self.manager.create_plan(
                request,
                revision_feedback=reply.text or "Revise the plan.",
            )
            self._emit(
                "planned", "Revised plan ready", detail=f"{len(plan.tasks)} task(s)"
            )

    async def _develop_task(self, task: Task, design: Design) -> TaskResult:
        """Implement, review, and test a single task with retries."""

        feedback: Optional[Review] = None
        implementation = None
        review = None
        test_report = None
        attempts = 0

        while attempts < self.config.max_task_attempts:
            attempts += 1
            task.status = TaskStatus.IN_PROGRESS
            self._emit(
                "implement", f"Implementing {task.id}", detail=f"attempt {attempts}"
            )
            implementation = await self.engineer.implement(task, design, feedback)

            task.status = TaskStatus.IN_REVIEW
            review = await self.reviewer.review(task, implementation)
            if not review.approved:
                task.status = TaskStatus.CHANGES_REQUESTED
                self._emit(
                    "changes-requested",
                    f"{task.id} sent back",
                    detail=review.summary,
                )
                feedback = review
                continue

            task.status = TaskStatus.TESTING
            test_report = await self.qa.test(task, implementation)
            if not self._tests_pass(test_report):
                task.status = TaskStatus.CHANGES_REQUESTED
                self._emit(
                    "tests-failed",
                    f"{task.id} failed QA",
                    detail=test_report.summary,
                )
                feedback = self._feedback_from_tests(test_report)
                continue

            task.status = TaskStatus.DONE
            self._emit("task-done", f"{task.id} done", detail=f"{attempts} attempt(s)")
            return TaskResult(
                task=task,
                attempts=attempts,
                implementation=implementation,
                review=review,
                test_report=test_report,
            )

        task.status = TaskStatus.FAILED
        self._emit(
            "task-failed",
            f"{task.id} failed",
            detail=f"after {attempts} attempt(s)",
        )
        return TaskResult(
            task=task,
            attempts=attempts,
            implementation=implementation,
            review=review,
            test_report=test_report,
        )
