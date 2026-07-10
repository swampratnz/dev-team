"""dev-team: a multi-agent software development team on the Claude Agent SDK.

The package coordinates a roster of role-specialised agents — product manager,
architect, engineer, reviewer, QA, security, technical writer, SRE, and DevOps —
through the full software development lifecycle for a feature request.

Two engines are available:

* :class:`DevelopmentWorkflow` / :meth:`DevTeam.develop` — a fast, side-effect
  free *simulation* of the lifecycle.
* :class:`DeliveryEngine` / :meth:`DevTeam.deliver` — *real* delivery that writes
  a workspace, runs executable quality gates, schedules tasks concurrently,
  commits via git, and threads budget, tracing, memory, and approvals through.
"""

from __future__ import annotations

from .approval import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    AutoApprover,
    CallbackApprovalGate,
    DenyAll,
    PolicyApprovalGate,
)
from .backlog import Backlog, BacklogStore, Epic, Iteration, ItemStatus, Story
from .budget import Budget, BudgetExceededError, UsageMeter, UsageRecord
from .changes import AppliedChange, ApplyResult, ChangeApplier
from .config import TeamConfig
from .engine import DeliveryEngine, DeliveryOutcome, EngineConfig
from .errors import (
    AgentResponseError,
    DependencyCycleError,
    DevTeamError,
    JSONExtractionError,
    WorkflowError,
)
from .evals import EvalCase, EvalReport, EvalResult, evaluate
from .events import AgentEvent
from .execution import (
    CommandResult,
    CommandRunner,
    DryRunCommandRunner,
    FakeCommandRunner,
    InMemoryWorkspace,
    LocalWorkspace,
    SubprocessCommandRunner,
    Workspace,
    WorkspaceError,
)
from .git import GitError, GitRepo
from .instrument import InstrumentedRunner
from .memory import (
    Artifact,
    Blackboard,
    CheckpointStore,
    DecisionRecord,
    ProjectMemory,
    RunCheckpoint,
)
from .models import (
    ChangeType,
    Design,
    DesignComponent,
    DeploymentPlan,
    DocSection,
    Documentation,
    FeatureRequest,
    FileChange,
    Implementation,
    Plan,
    ProjectResult,
    ReliabilityReport,
    Review,
    ReviewComment,
    SecurityFinding,
    SecurityReport,
    Severity,
    Task,
    TaskResult,
    TaskStatus,
    TestCase,
    TestKind,
    TestReport,
)
from .policy import GuardedCommandRunner, PolicyVerdict, SideEffectPolicy
from .scheduler import ScheduledResult, ScheduleStatus, schedule
from .sdk import AgentResult, AgentRunner, ClaudeAgentRunner
from .team import DevTeam, build_workflow
from .trace import Tracer, TraceSpan
from .verification import (
    CommandGate,
    CoverageGate,
    DefinitionOfDone,
    DoDReport,
    Gate,
    GateContext,
    GateResult,
    PredicateGate,
)
from .workflow import DevelopmentWorkflow

__version__ = "0.3.0"

__all__ = [
    "__version__",
    # facade & engines
    "DevTeam",
    "build_workflow",
    "DevelopmentWorkflow",
    "DeliveryEngine",
    "DeliveryOutcome",
    "EngineConfig",
    "TeamConfig",
    # sdk boundary
    "AgentEvent",
    "AgentResult",
    "AgentRunner",
    "ClaudeAgentRunner",
    "InstrumentedRunner",
    # execution
    "Workspace",
    "InMemoryWorkspace",
    "LocalWorkspace",
    "WorkspaceError",
    "CommandRunner",
    "CommandResult",
    "SubprocessCommandRunner",
    "DryRunCommandRunner",
    "FakeCommandRunner",
    "ChangeApplier",
    "ApplyResult",
    "AppliedChange",
    "GitRepo",
    "GitError",
    # governance
    "Budget",
    "BudgetExceededError",
    "UsageMeter",
    "UsageRecord",
    "Tracer",
    "TraceSpan",
    "ApprovalGate",
    "ApprovalRequest",
    "ApprovalDecision",
    "AutoApprover",
    "DenyAll",
    "PolicyApprovalGate",
    "CallbackApprovalGate",
    "SideEffectPolicy",
    "GuardedCommandRunner",
    "PolicyVerdict",
    # memory
    "Blackboard",
    "Artifact",
    "DecisionRecord",
    "ProjectMemory",
    "CheckpointStore",
    "RunCheckpoint",
    # evals
    "EvalCase",
    "EvalResult",
    "EvalReport",
    "evaluate",
    # verification
    "Gate",
    "GateContext",
    "GateResult",
    "CommandGate",
    "CoverageGate",
    "PredicateGate",
    "DefinitionOfDone",
    "DoDReport",
    # scheduling
    "schedule",
    "ScheduleStatus",
    "ScheduledResult",
    # delivery / backlog
    "Backlog",
    "BacklogStore",
    "Epic",
    "Story",
    "Iteration",
    "ItemStatus",
    # errors
    "DevTeamError",
    "AgentResponseError",
    "DependencyCycleError",
    "JSONExtractionError",
    "WorkflowError",
    # models
    "FeatureRequest",
    "Task",
    "TaskStatus",
    "Plan",
    "Design",
    "DesignComponent",
    "FileChange",
    "ChangeType",
    "Implementation",
    "Review",
    "ReviewComment",
    "Severity",
    "TestCase",
    "TestKind",
    "TestReport",
    "DeploymentPlan",
    "TaskResult",
    "ProjectResult",
    "SecurityFinding",
    "SecurityReport",
    "DocSection",
    "Documentation",
    "ReliabilityReport",
]
