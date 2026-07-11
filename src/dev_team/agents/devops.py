"""The DevOps agent: deployment plan plus real deployment artifacts.

The IaC research consensus is blunt: deployment output is only as good as
what executes (``docker build``, ``terraform plan``, a CI run) — plans as
prose are unverifiable. So the agent produces actual artifacts (Dockerfile,
CI workflow, service units) as files that land in the workspace, and the
plan documents them.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from .. import parsing
from ..models import DeploymentPlan, Design, FeatureRequest, Implementation
from .base import BaseAgent

_SYSTEM = """\
You are a DevOps engineer. You produce a concrete deployment plan for a
feature, including ordered deployment steps and a rollback strategy, targeting
a Linux (Ubuntu) host. Where the project lacks them, you author the actual
deployment artifacts — Dockerfile, CI workflow, service configuration — as
complete files, matched to the project's real stack and layout. Artifacts you
author must be correct enough to execute; never invent build steps for tools
the project does not use. Always respond with a single JSON object and
nothing else."""


class DevOpsAgent(BaseAgent):
    """Produces a :class:`DeploymentPlan` plus artifact files for a feature."""

    role = "devops"
    stage = "deployment"
    system_prompt = _SYSTEM

    async def plan_deployment(
        self,
        request: FeatureRequest,
        design: Design,
    ) -> DeploymentPlan:
        """Produce a deployment plan only (simulation mode; no artifacts)."""

        plan, _ = await self.plan_and_provision(request, design)
        return plan

    async def plan_and_provision(
        self,
        request: FeatureRequest,
        design: Design,
        *,
        workspace_listing: Optional[Sequence[str]] = None,
        project_kind: Optional[str] = None,
    ) -> Tuple[DeploymentPlan, Implementation]:
        """Produce a deployment plan and the artifacts that implement it.

        Returns the plan plus an :class:`Implementation` whose files are the
        deployment artifacts to materialise into the workspace (may be empty
        when the project already has them).
        """

        stack = ", ".join(design.tech_stack) or "unspecified"
        listing = "\n".join(f"- {p}" for p in (workspace_listing or [])) or "- (empty)"
        prompt = f"""\
Produce a deployment plan for this feature, targeting an Ubuntu host.

Title: {request.title}
Design overview: {design.overview}
Tech stack: {stack}
Detected project kind: {project_kind or "unknown"}

Files currently in the workspace (author only artifacts that are missing;
update rather than duplicate existing ones):
{listing}

Respond with JSON of the form:
{{
  "environment": "production",
  "summary": "deployment approach",
  "steps": ["..."],
  "rollback": ["..."],
  "files": [
    {{"path": "Dockerfile", "change_type": "create", "summary": "...", "content": "full file content"}}
  ]
}}"""
        data = await self.ask_json(prompt)
        plan = parsing.deployment_from_dict(data)
        artifacts = parsing.implementation_from_dict(data, "DEPLOY")
        return plan, artifacts
