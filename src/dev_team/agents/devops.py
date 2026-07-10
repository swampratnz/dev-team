"""The DevOps agent: plans deployment and rollback."""

from __future__ import annotations

from .. import parsing
from ..models import Design, DeploymentPlan, FeatureRequest
from .base import BaseAgent

_SYSTEM = """\
You are a DevOps engineer. You produce a concrete deployment plan for a feature,
including ordered deployment steps and a rollback strategy, targeting a Linux
(Ubuntu) host. Always respond with a single JSON object and nothing else."""


class DevOpsAgent(BaseAgent):
    """Produces a :class:`DeploymentPlan` for a feature."""

    role = "devops"
    stage = "deployment"
    system_prompt = _SYSTEM

    async def plan_deployment(
        self,
        request: FeatureRequest,
        design: Design,
    ) -> DeploymentPlan:
        """Produce a deployment plan for ``request`` given ``design``."""

        stack = ", ".join(design.tech_stack) or "unspecified"
        prompt = f"""\
Produce a deployment plan for this feature, targeting an Ubuntu host.

Title: {request.title}
Design overview: {design.overview}
Tech stack: {stack}

Respond with JSON of the form:
{{
  "environment": "production",
  "summary": "deployment approach",
  "steps": ["..."],
  "rollback": ["..."]
}}"""
        data = await self.ask_json(prompt)
        return parsing.deployment_from_dict(data)
