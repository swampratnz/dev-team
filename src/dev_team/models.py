"""Domain models describing the software development lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TaskStatus(str, Enum):
    """Lifecycle status of a single development task."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    TESTING = "testing"
    DONE = "done"
    FAILED = "failed"


class Severity(str, Enum):
    """Severity of a review comment."""

    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class ChangeType(str, Enum):
    """Kind of change applied to a file."""

    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class TestKind(str, Enum):
    """Category of an automated test."""

    UNIT = "unit"
    INTEGRATION = "integration"
    E2E = "e2e"


@dataclass
class FeatureRequest:
    """A unit of work requested from the development team."""

    title: str
    description: str
    constraints: List[str] = field(default_factory=list)


@dataclass
class Task:
    """A single, independently reviewable piece of work."""

    id: str
    title: str
    description: str
    acceptance_criteria: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class Plan:
    """A decomposition of a feature request into ordered tasks."""

    summary: str
    tasks: List[Task] = field(default_factory=list)


@dataclass
class DesignComponent:
    """A component identified during architectural design."""

    name: str
    responsibility: str


@dataclass
class Design:
    """The technical design for a feature.

    ``alternatives`` and ``rationale`` capture ATAM-style tradeoff analysis:
    what other approaches were considered and why this one won. A design
    without rejected alternatives usually means no alternatives were weighed.
    """

    overview: str
    components: List[DesignComponent] = field(default_factory=list)
    tech_stack: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    alternatives: List[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class FileChange:
    """A single file mutation produced by the engineer."""

    path: str
    change_type: ChangeType
    summary: str
    content: str = ""


@dataclass
class Implementation:
    """The engineer's output for a task."""

    task_id: str
    summary: str
    files: List[FileChange] = field(default_factory=list)
    notes: str = ""


@dataclass
class ReviewComment:
    """A single reviewer remark."""

    severity: Severity
    message: str
    path: Optional[str] = None


@dataclass
class Review:
    """The reviewer's verdict on an implementation."""

    approved: bool
    summary: str
    comments: List[ReviewComment] = field(default_factory=list)

    @property
    def blocking_comments(self) -> List[ReviewComment]:
        """Comments severe enough to block approval."""

        return [
            c
            for c in self.comments
            if c.severity in (Severity.MAJOR, Severity.CRITICAL)
        ]


@dataclass
class TestCase:
    """A single automated test authored by QA."""

    name: str
    kind: TestKind
    target: str


@dataclass
class TestReport:
    """QA's report for a task's implementation."""

    passed: bool
    coverage: float
    summary: str
    cases: List[TestCase] = field(default_factory=list)


@dataclass
class DeploymentPlan:
    """DevOps' plan for shipping the feature."""

    environment: str
    summary: str
    steps: List[str] = field(default_factory=list)
    rollback: List[str] = field(default_factory=list)


@dataclass
class SecurityFinding:
    """A single issue raised by the security engineer."""

    severity: Severity
    category: str
    description: str
    remediation: str = ""


@dataclass
class SecurityReport:
    """The security engineer's assessment of an implementation."""

    approved: bool
    summary: str
    findings: List[SecurityFinding] = field(default_factory=list)
    # Set only by the engine from a deterministic scan-command exit-code
    # check — never by the LLM — when a configured scanner did not actually
    # run (binary missing, timed out), so that gets surfaced as distinct
    # from "scanner ran clean" rather than silently trusted.
    scanner_failed: bool = False
    scanner_error: Optional[str] = None

    @property
    def blocking_findings(self) -> List[SecurityFinding]:
        """Findings severe enough to block a release."""

        return [
            f
            for f in self.findings
            if f.severity in (Severity.MAJOR, Severity.CRITICAL)
        ]


@dataclass
class DocSection:
    """A single documentation section."""

    title: str
    content: str


@dataclass
class Documentation:
    """The technical writer's output for a feature."""

    summary: str
    sections: List[DocSection] = field(default_factory=list)


@dataclass
class ReliabilityReport:
    """The SRE's production-readiness assessment."""

    production_ready: bool
    summary: str
    slos: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    runbook: List[str] = field(default_factory=list)


@dataclass
class TaskResult:
    """The full outcome of developing a single task."""

    task: Task
    attempts: int
    implementation: Optional[Implementation] = None
    review: Optional[Review] = None
    test_report: Optional[TestReport] = None

    @property
    def succeeded(self) -> bool:
        """Whether the task reached a fully done state."""

        return self.task.status is TaskStatus.DONE


@dataclass
class ProjectResult:
    """The aggregate result of a full development run.

    ``cost_usd`` is the total metered agent spend for the run (populated from
    the simulation's :class:`~dev_team.budget.UsageMeter`); it defaults to
    ``0.0`` so results built without cost metering — and existing callers that
    never set it — stay valid.
    """

    request: FeatureRequest
    plan: Plan
    design: Design
    task_results: List[TaskResult] = field(default_factory=list)
    deployment: Optional[DeploymentPlan] = None
    cost_usd: float = 0.0

    @property
    def success(self) -> bool:
        """True only when every task succeeded."""

        return bool(self.task_results) and all(
            tr.succeeded for tr in self.task_results
        )

    @property
    def completed_tasks(self) -> List[TaskResult]:
        """Task results that reached a done state."""

        return [tr for tr in self.task_results if tr.succeeded]

    @property
    def failed_tasks(self) -> List[TaskResult]:
        """Task results that did not reach a done state."""

        return [tr for tr in self.task_results if not tr.succeeded]
