"""The :class:`DevTeam` facade: the main entry point to the system."""

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
from .config import TeamConfig
from .engine import DeliveryEngine, DeliveryOutcome
from .events import Listener
from .models import FeatureRequest, ProjectResult
from .sdk import AgentRunner, ClaudeAgentRunner
from .workflow import DevelopmentWorkflow


def build_workflow(
    runner: AgentRunner,
    *,
    config: Optional[TeamConfig] = None,
    listener: Optional[Listener] = None,
) -> DevelopmentWorkflow:
    """Construct a :class:`DevelopmentWorkflow` with the full agent roster."""

    config = config or TeamConfig()
    model = config.model

    def make(agent_cls):
        return agent_cls(runner, model=model, listener=listener)

    return DevelopmentWorkflow(
        manager=make(ProductManagerAgent),
        architect=make(ArchitectAgent),
        engineer=make(EngineerAgent),
        reviewer=make(ReviewerAgent),
        qa=make(QAAgent),
        devops=make(DevOpsAgent),
        config=config,
        listener=listener,
    )


class DevTeam:
    """A self-contained multi-agent software development team.

    Example:
        >>> team = DevTeam(runner)                      # doctest: +SKIP
        >>> result = await team.develop_feature(        # doctest: +SKIP
        ...     "Login", "Add email/password login")
    """

    def __init__(
        self,
        runner: Optional[AgentRunner] = None,
        *,
        config: Optional[TeamConfig] = None,
        listener: Optional[Listener] = None,
    ) -> None:
        self.config = config or TeamConfig()
        self.listener = listener
        self.runner = runner or ClaudeAgentRunner(
            default_model=self.config.model,
            permission_mode=self.config.permission_mode,
            cwd=self.config.working_dir,
            max_turns=self.config.max_turns,
        )
        self.workflow = build_workflow(
            self.runner, config=self.config, listener=listener
        )

    async def develop(self, request: FeatureRequest) -> ProjectResult:
        """Run the full development lifecycle for ``request``."""

        return await self.workflow.run(request)

    async def develop_feature(
        self,
        title: str,
        description: str,
        constraints: Optional[List[str]] = None,
    ) -> ProjectResult:
        """Convenience wrapper building a :class:`FeatureRequest` inline."""

        request = FeatureRequest(
            title=title,
            description=description,
            constraints=list(constraints) if constraints else [],
        )
        return await self.develop(request)

    def make_engine(self, **kwargs) -> DeliveryEngine:
        """Build a :class:`DeliveryEngine` for real, gated, side-effecting runs.

        Keyword arguments are forwarded to :class:`DeliveryEngine` (e.g.
        ``workspace``, ``command_runner``, ``config``, ``budget``, ``tracer``).
        """

        kwargs.setdefault("listener", self.listener)
        return DeliveryEngine(self.runner, **kwargs)

    async def deliver(self, request: FeatureRequest, **kwargs) -> DeliveryOutcome:
        """Run the real delivery engine for ``request``."""

        return await self.make_engine(**kwargs).deliver(request)
