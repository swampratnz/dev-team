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
    ASSESSMENT_JSON_PATH,
    DEFAULT_EXCLUDE_GLOBS,
    VERIFY_VERDICTS,
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
    dict_to_backlog,
    find_finding,
    inventory_stats,
    list_findings,
    outcome_to_backlog,
    outcome_to_dict,
    render_report,
    run_build_probe,
    verify_finding,
)
from .conventions import (
    ConventionsProfile,
    ConventionsStore,
    detect_convention_sources,
)
from .dashboard import DashboardServer, collect_state
from .dispatch import Dispatcher, DispatchServer, JobRecord, JobSpec
from .eventlog import EventLog, compose, read_events
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
from .delivery_target import DeliveryTargetError, publish_pull_request, push_branch
from .engine import DeliveryEngine, DeliveryOutcome, EngineConfig, RemediationOutcome
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
from .instrument import InstrumentedRunner, InstrumentedSession
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
from .scores import RunScore, ScoreHistory
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
from .checks import (
    ChecksError,
    ChecksOutcome,
    ChecksReader,
    GitHubChecksReader,
    watch_checks,
)
from .pullrequest import (
    FakePullRequestPublisher,
    GitHubPullRequestPublisher,
    PullRequest,
    PullRequestError,
    PullRequestPublisher,
    PullRequestRequest,
)
from .replan import Replan, ReplanAction, ReplanError, apply_replan
from .retrieval import (
    Retrieval,
    RetrievedFile,
    char_budget_for_tokens,
    estimate_tokens,
    retrieve,
)
from .sandbox import ContainerCommandRunner, SandboxConfig, SandboxError
from .visualreview import (
    VISUAL_RUBRIC,
    AnthropicVisualReviewer,
    AppServer,
    FakeAppServer,
    FakePageCapturer,
    FakeVisualReviewer,
    PageCapturer,
    PlaywrightPageCapturer,
    Screenshot,
    SubprocessAppServer,
    VisualFinding,
    VisualReport,
    VisualReviewer,
)
from .scheduler import ScheduledResult, ScheduleStatus, schedule
from .sdk import (
    AgentResult,
    AgentRunner,
    AgentSession,
    ClaudeAgentRunner,
    ClaudeAgentSession,
    FakeAgentSession,
)
from .sources import (
    RepoRef,
    SourceError,
    clone_or_update,
    default_env_file,
    git_auth_env,
    load_env_file,
    parse_repo,
    resolve_github_token,
    scrub_credentials,
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
    "RemediationOutcome",
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
    "dict_to_backlog",
    "ASSESSMENT_JSON_PATH",
    "list_findings",
    "find_finding",
    "verify_finding",
    "VERIFY_VERDICTS",
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
    "AgentSession",
    "ClaudeAgentSession",
    "FakeAgentSession",
    "InstrumentedRunner",
    "InstrumentedSession",
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
    "ContainerCommandRunner",
    "SandboxConfig",
    "SandboxError",
    "Screenshot",
    "VisualFinding",
    "VisualReport",
    "AppServer",
    "PageCapturer",
    "VisualReviewer",
    "FakeAppServer",
    "FakePageCapturer",
    "FakeVisualReviewer",
    "SubprocessAppServer",
    "PlaywrightPageCapturer",
    "AnthropicVisualReviewer",
    "VISUAL_RUBRIC",
    "Replan",
    "ReplanAction",
    "ReplanError",
    "apply_replan",
    "Retrieval",
    "RetrievedFile",
    "retrieve",
    "estimate_tokens",
    "char_budget_for_tokens",
    "PullRequest",
    "PullRequestRequest",
    "PullRequestPublisher",
    "GitHubPullRequestPublisher",
    "FakePullRequestPublisher",
    "PullRequestError",
    "publish_pull_request",
    "push_branch",
    "ChecksOutcome",
    "ChecksReader",
    "GitHubChecksReader",
    "ChecksError",
    "watch_checks",
    "DeliveryTargetError",
    # memory
    "Blackboard",
    "Artifact",
    "DecisionRecord",
    "ProjectMemory",
    "CheckpointStore",
    "RunCheckpoint",
    "ScoreHistory",
    "RunScore",
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
    # dashboard & event journal
    "DashboardServer",
    "collect_state",
    "EventLog",
    "read_events",
    "compose",
    # dispatch service
    "DispatchServer",
    "Dispatcher",
    "JobSpec",
    "JobRecord",
    # repository sources
    "RepoRef",
    "SourceError",
    "parse_repo",
    "clone_or_update",
    "default_env_file",
    "load_env_file",
    "resolve_github_token",
    "git_auth_env",
    "scrub_credentials",
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
