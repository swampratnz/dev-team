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
from .assessment import (
    DEFAULT_EXCLUDE_GLOBS,
    AssessConfig,
    AssessmentEngine,
    AssessmentOutcome,
    BuildProbe,
    Component,
    InventoryStats,
    PhaseResult,
    ProbeCommandResult,
    audit_blind_spots,
    detect_components,
    inventory_stats,
    outcome_to_backlog,
    outcome_to_dict,
    render_report,
    run_build_probe,
)
from .conventions import (
    ConventionsProfile,
    ConventionsStore,
    detect_convention_sources,
)
from .deadcode import DeadCodeFinding, DeadCodeReport, detect_dead_code
from .depscan import (
    Dependency,
    DependencyScan,
    Vulnerability,
    collect_dependencies,
    scan_dependencies,
)
from .backlog import Backlog, BacklogStore, Epic, Iteration, ItemStatus, Story
from .budget import Budget, BudgetExceededError, UsageMeter, UsageRecord
from .changes import AppliedChange, ApplyResult, ChangeApplier
from .config import TeamConfig
from .context import RepoContext, build_repo_context
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
from .failures import new_failures, parse_failed_tests
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
from .interaction import (
    AutoChannel,
    ChannelApprovalGate,
    Choice,
    ConsoleChannel,
    InteractionChannel,
    Question,
    QueueChannel,
    Reply,
    ScriptedChannel,
)
from .memory import (
    Artifact,
    Blackboard,
    CheckpointStore,
    DecisionRecord,
    ProjectMemory,
    RunCheckpoint,
)
from .ordering import lint_plan, topological_order
from .persona import DEFAULT_CAST, Persona, Roster
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
from .profile import ProjectProfile, detect_project
from .scheduler import ScheduledResult, ScheduleStatus, schedule
from .sdk import AgentResult, AgentRunner, ClaudeAgentRunner
from .sources import (
    RepoRef,
    SourceError,
    clone_or_update,
    load_env_file,
    parse_repo,
    resolve_github_token,
)
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
    RemoteCIGate,
)
from .workflow import DevelopmentWorkflow

__version__ = "0.7.0"

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
    "AssessmentEngine",
    "AssessmentOutcome",
    "AssessConfig",
    "PhaseResult",
    "InventoryStats",
    "inventory_stats",
    "outcome_to_dict",
    "outcome_to_backlog",
    "render_report",
    "Component",
    "detect_components",
    "DEFAULT_EXCLUDE_GLOBS",
    "BuildProbe",
    "ProbeCommandResult",
    "run_build_probe",
    "audit_blind_spots",
    # dead code, dependency scanning, conventions
    "DeadCodeFinding",
    "DeadCodeReport",
    "detect_dead_code",
    "Dependency",
    "DependencyScan",
    "Vulnerability",
    "collect_dependencies",
    "scan_dependencies",
    "ConventionsProfile",
    "ConventionsStore",
    "detect_convention_sources",
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
    # interactivity & personas
    "InteractionChannel",
    "Question",
    "Choice",
    "Reply",
    "AutoChannel",
    "ConsoleChannel",
    "QueueChannel",
    "ScriptedChannel",
    "ChannelApprovalGate",
    "Persona",
    "Roster",
    "DEFAULT_CAST",
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
    # project profile & context
    "ProjectProfile",
    "detect_project",
    "RepoContext",
    "build_repo_context",
    "parse_failed_tests",
    "new_failures",
    # repository sources
    "RepoRef",
    "SourceError",
    "parse_repo",
    "clone_or_update",
    "load_env_file",
    "resolve_github_token",
    # verification
    "Gate",
    "GateContext",
    "GateResult",
    "CommandGate",
    "CoverageGate",
    "PredicateGate",
    "RemoteCIGate",
    "DefinitionOfDone",
    "DoDReport",
    # scheduling & planning
    "schedule",
    "ScheduleStatus",
    "ScheduledResult",
    "lint_plan",
    "topological_order",
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
