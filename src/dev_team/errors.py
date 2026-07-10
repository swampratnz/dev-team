"""Exception hierarchy for the dev-team system."""

from __future__ import annotations


class DevTeamError(Exception):
    """Base class for all dev-team errors."""


class JSONExtractionError(DevTeamError):
    """Raised when a JSON payload cannot be extracted from model output."""

    def __init__(self, text: str) -> None:
        preview = text.strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        super().__init__(f"Could not extract JSON from model output: {preview!r}")
        self.text = text


class AgentResponseError(DevTeamError):
    """Raised when an agent returns a response that cannot be parsed."""

    def __init__(self, role: str, text: str) -> None:
        preview = text.strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        super().__init__(f"Agent {role!r} returned an unusable response: {preview!r}")
        self.role = role
        self.text = text


class DependencyCycleError(DevTeamError):
    """Raised when tasks form a dependency cycle and cannot be ordered."""

    def __init__(self, task_ids: list[str]) -> None:
        joined = ", ".join(task_ids)
        super().__init__(f"Dependency cycle detected among tasks: {joined}")
        self.task_ids = task_ids


class WorkflowError(DevTeamError):
    """Raised when the development workflow cannot proceed."""
