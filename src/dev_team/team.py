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
from .assessment import AssessmentEngine, AssessmentOutcome
from .config import TeamConfig
from .engine import DeliveryEngine, DeliveryOutcome
from .events import Listener
from .interaction import InteractionChannel
from .models import FeatureRequest, ProjectResult
from .persona import Roster
from .sdk import AgentRunner, ClaudeAgentRunner
from .workflow import DevelopmentWorkflow


def build_workflow(
    runner: AgentRunner,
    *,
    config: Optional[TeamConfig] = None,
    listener: Optional[Listener] = None,
    roster: Optional[Roster] = None,
    interaction: Optional[InteractionChannel] = None,
) -> DevelopmentWorkflow:
    """Construct a :class:`DevelopmentWorkflow` with the full agent roster."""

    config = config or TeamConfig()
    model = config.model
    cast = roster if roster is not None else Roster.default()

    def make(agent_cls):
        return agent_cls(
            runner,
            model=model,
            listener=listener,
            persona=cast.get(agent_cls.role),
        )

    return DevelopmentWorkflow(
        manager=make(ProductManagerAgent),
        architect=make(ArchitectAgent),
        engineer=make(EngineerAgent),
        reviewer=make(ReviewerAgent),
        qa=make(QAAgent),
        devops=make(DevOpsAgent),
        config=config,
        listener=listener,
        interaction=interaction,
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
        roster: Optional[Roster] = None,
        interaction: Optional[InteractionChannel] = None,
    ) -> None:
        self.config = config or TeamConfig()
        self.listener = listener
        self.roster = roster if roster is not None else Roster.default()
        self.interaction = interaction
        self.runner = runner or ClaudeAgentRunner(
            default_model=self.config.model,
            permission_mode=self.config.permission_mode,
            cwd=self.config.working_dir,
            max_turns=self.config.max_turns,
        )
        self.workflow = build_workflow(
            self.runner,
            config=self.config,
            listener=listener,
            roster=self.roster,
            interaction=interaction,
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
        kwargs.setdefault("roster", self.roster)
        kwargs.setdefault("interaction", self.interaction)
        return DeliveryEngine(self.runner, **kwargs)

    async def deliver(self, request: FeatureRequest, **kwargs) -> DeliveryOutcome:
        """Run the real delivery engine for ``request``."""

        return await self.make_engine(**kwargs).deliver(request)

    def make_assessor(self, **kwargs) -> AssessmentEngine:
        """Build an :class:`AssessmentEngine` for a read-only repository audit.

        Keyword arguments are forwarded to :class:`AssessmentEngine` (e.g.
        ``workspace``, ``config``, ``budget``, ``tracer``).
        """

        kwargs.setdefault("listener", self.listener)
        kwargs.setdefault("roster", self.roster)
        kwargs.setdefault("interaction", self.interaction)
        return AssessmentEngine(self.runner, **kwargs)

    async def assess(self, **kwargs) -> AssessmentOutcome:
        """Audit a repository read-only and return the assessment."""

        return await self.make_assessor(**kwargs).assess()
