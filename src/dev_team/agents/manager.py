"""The product manager agent: turns a request into a plan of tasks.

Decomposition quality is what downstream success is built on, so the plan is
held to an INVEST-style bar: tasks independently shippable and small, with
acceptance criteria phrased so an automated test can verify them. The engine
lints the plan (see :func:`~dev_team.ordering.lint_plan`) and asks for one
revision when it falls short.
"""

from __future__ import annotations

from typing import Optional

from .. import parsing
from ..models import FeatureRequest, Plan
from .base import BaseAgent

_SYSTEM = """\
You are an experienced product manager and delivery lead. You break feature
requests into small, independently shippable engineering tasks with explicit
dependencies. Every task's acceptance criteria are objectively verifiable —
phrased so an automated test could assert each one (inputs, outputs, observable
behaviour), never vague qualities like "works well". Tasks follow INVEST:
independent, negotiable, valuable, estimable, small, testable.
Always respond with a single JSON object and nothing else."""


class ProductManagerAgent(BaseAgent):
    """Decomposes a :class:`FeatureRequest` into a :class:`Plan`."""

    role = "product-manager"
    stage = "planning"
    system_prompt = _SYSTEM

    async def create_plan(
        self,
        request: FeatureRequest,
        *,
        prior_context: Optional[str] = None,
        revision_feedback: Optional[str] = None,
    ) -> Plan:
        """Produce a task breakdown for ``request``.

        ``prior_context`` carries what the team remembers from earlier runs on
        this workspace (decisions, artifacts, retrospectives) plus the repo
        map. ``revision_feedback`` is set when a previous plan failed lint —
        the plan must be re-issued with those problems fixed.
        """

        constraints = (
            "\n".join(f"- {c}" for c in request.constraints)
            if request.constraints
            else "- none"
        )
        memory = (
            f"\nContext from previous runs on this workspace:\n{prior_context}\n"
            if prior_context
            else ""
        )
        revision = (
            "\nYour previous plan had these problems — fix all of them:\n"
            f"{revision_feedback}\n"
            if revision_feedback
            else ""
        )
        prompt = f"""\
Break the following feature request into engineering tasks.

Title: {request.title}
Description:
{request.description}

Constraints:
{constraints}
{memory}{revision}
Respond with JSON of the form:
{{
  "summary": "one paragraph plan summary",
  "tasks": [
    {{
      "id": "T1",
      "title": "short title",
      "description": "what to build",
      "acceptance_criteria": ["objectively verifiable criterion"],
      "dependencies": ["T0"]
    }}
  ]
}}"""
        data = await self.ask_json(prompt)
        return parsing.plan_from_dict(data)
