"""The architect agent: produces a technical design."""

from __future__ import annotations

from .. import parsing
from ..models import Design, FeatureRequest, Plan
from .base import BaseAgent

_SYSTEM = """\
You are a pragmatic software architect. You turn a plan into a concise technical
design: the components involved, their responsibilities, the technology choices,
and the key risks. Always respond with a single JSON object and nothing else."""


class ArchitectAgent(BaseAgent):
    """Produces a :class:`Design` for a request and its plan."""

    role = "architect"
    stage = "design"
    system_prompt = _SYSTEM

    async def design(self, request: FeatureRequest, plan: Plan) -> Design:
        """Produce a technical design for ``request`` given ``plan``."""

        task_lines = "\n".join(
            f"- {task.id}: {task.title}" for task in plan.tasks
        ) or "- (no tasks)"
        prompt = f"""\
Design the technical solution for this feature.

Title: {request.title}
Description:
{request.description}

Planned tasks:
{task_lines}

Respond with JSON of the form:
{{
  "overview": "high level approach",
  "components": [{{"name": "...", "responsibility": "..."}}],
  "tech_stack": ["..."],
  "risks": ["..."]
}}"""
        data = await self.ask_json(prompt)
        return parsing.design_from_dict(data)
