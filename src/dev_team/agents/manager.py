"""The product manager agent: turns a request into a plan of tasks."""

from __future__ import annotations

from .. import parsing
from ..models import FeatureRequest, Plan
from .base import BaseAgent

_SYSTEM = """\
You are an experienced product manager and delivery lead. You break feature
requests into small, independently shippable engineering tasks with clear
acceptance criteria and explicit dependencies between tasks.
Always respond with a single JSON object and nothing else."""


class ProductManagerAgent(BaseAgent):
    """Decomposes a :class:`FeatureRequest` into a :class:`Plan`."""

    role = "product-manager"
    stage = "planning"
    system_prompt = _SYSTEM

    async def create_plan(self, request: FeatureRequest) -> Plan:
        """Produce a task breakdown for ``request``."""

        constraints = (
            "\n".join(f"- {c}" for c in request.constraints)
            if request.constraints
            else "- none"
        )
        prompt = f"""\
Break the following feature request into engineering tasks.

Title: {request.title}
Description:
{request.description}

Constraints:
{constraints}

Respond with JSON of the form:
{{
  "summary": "one paragraph plan summary",
  "tasks": [
    {{
      "id": "T1",
      "title": "short title",
      "description": "what to build",
      "acceptance_criteria": ["..."],
      "dependencies": ["T0"]
    }}
  ]
}}"""
        data = await self.ask_json(prompt)
        return parsing.plan_from_dict(data)
