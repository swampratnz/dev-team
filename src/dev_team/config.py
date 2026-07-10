"""Configuration for a development team run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TeamConfig:
    """Tunable settings for a :class:`~dev_team.team.DevTeam`.

    Attributes:
        model: Model identifier passed to the Agent SDK (``None`` uses the
            SDK/CLI default).
        max_task_attempts: How many times the engineer may re-attempt a task
            before it is marked failed. Must be at least 1.
        min_coverage: The minimum test coverage percentage QA must report for a
            task's tests to be considered passing.
        working_dir: Directory the agents operate in (passed to the SDK as
            ``cwd``). ``None`` uses the process working directory.
        permission_mode: Permission mode handed to the Agent SDK.
    """

    model: Optional[str] = None
    max_task_attempts: int = 2
    min_coverage: float = 100.0
    working_dir: Optional[str] = None
    permission_mode: str = "bypassPermissions"

    def __post_init__(self) -> None:
        if self.max_task_attempts < 1:
            raise ValueError("max_task_attempts must be at least 1")
        if not 0.0 <= self.min_coverage <= 100.0:
            raise ValueError("min_coverage must be between 0 and 100")
