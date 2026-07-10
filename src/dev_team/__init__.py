"""dev-team: a multi-agent software development team on the Claude Agent SDK.

The package coordinates a roster of role-specialised agents — product manager,
architect, engineer, reviewer, QA, and DevOps — through the full software
development lifecycle for a feature request.
"""

from __future__ import annotations

from .config import TeamConfig
from .errors import (
    AgentResponseError,
    DependencyCycleError,
    DevTeamError,
    JSONExtractionError,
    WorkflowError,
)
from .events import AgentEvent
from .models import (
    ChangeType,
    Design,
    DesignComponent,
    DeploymentPlan,
    FeatureRequest,
    FileChange,
    Implementation,
    Plan,
    ProjectResult,
    Review,
    ReviewComment,
    Severity,
    Task,
    TaskResult,
    TaskStatus,
    TestCase,
    TestKind,
    TestReport,
)
from .sdk import AgentResult, AgentRunner, ClaudeAgentRunner
from .team import DevTeam, build_workflow
from .workflow import DevelopmentWorkflow

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "TeamConfig",
    "DevTeam",
    "build_workflow",
    "DevelopmentWorkflow",
    "AgentEvent",
    "AgentResult",
    "AgentRunner",
    "ClaudeAgentRunner",
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
]
