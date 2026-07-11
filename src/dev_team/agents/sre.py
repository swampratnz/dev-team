"""The SRE agent: production-readiness review (SLOs, runbook, rollback).

Modelled on Google's SRE launch-review practice: a checklist-driven review
over *evidence* — the actual changed code, the gate results, and the
deployment plan — not a vibe check over a one-line design summary. Every
verdict must point at what it saw.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .. import parsing
from ..models import (
    DeploymentPlan,
    Design,
    FeatureRequest,
    Implementation,
    ReliabilityReport,
)
from .base import BaseAgent
from .reviewer import render_changed_files

_SYSTEM = """\
You are a site reliability engineer conducting a production-readiness review.
Work through this checklist against the evidence you are shown, and ground
every risk and every runbook step in something concrete you saw:
1. Failure modes: what breaks first under load, bad input, or dependency loss?
2. Observability: can an operator tell it is broken (logs, metrics, health)?
3. SLOs: what should be promised, and is it measurable in this code?
4. Runbook: concrete diagnose-and-mitigate steps an on-caller can follow.
5. Rollback: does the deployment plan's rollback actually undo this change?
You only mark production_ready=true when the checklist holds; cite evidence
for anything that fails it. Always respond with a single JSON object and
nothing else."""


class SREAgent(BaseAgent):
    """Produces a :class:`ReliabilityReport` for a feature."""

    role = "sre"
    stage = "reliability"
    system_prompt = _SYSTEM

    async def assess(
        self,
        request: FeatureRequest,
        design: Design,
        implementation: Optional[Implementation] = None,
        *,
        file_contents: Optional[Mapping[str, str]] = None,
        deployment: Optional[DeploymentPlan] = None,
        gate_summary: Optional[str] = None,
    ) -> ReliabilityReport:
        """Assess production readiness against the delivered evidence."""

        stack = ", ".join(design.tech_stack) or "unspecified"
        code = (
            render_changed_files(implementation, file_contents)
            if implementation is not None
            else "- (no code changes available)"
        )
        rollback = (
            "\n".join(f"- {step}" for step in deployment.rollback)
            if deployment is not None and deployment.rollback
            else "- (no rollback plan provided)"
        )
        gates = gate_summary or "(no gate results available)"
        prompt = f"""\
Conduct a production-readiness review for this feature.

Title: {request.title}
Design overview: {design.overview}
Tech stack: {stack}
Quality gate results: {gates}

Delivered code:
{code}

Deployment rollback plan (validate it against the change):
{rollback}

Respond with JSON of the form:
{{
  "production_ready": true,
  "summary": "readiness verdict citing the checklist",
  "slos": ["measurable objective"],
  "risks": ["failure mode grounded in the code shown"],
  "runbook": ["concrete operator step"]
}}"""
        data = await self.ask_json(prompt)
        return parsing.reliability_from_dict(data)
