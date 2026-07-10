"""The SRE agent: production-readiness review (SLOs, runbook, rollback)."""

from __future__ import annotations

from .. import parsing
from ..models import Design, FeatureRequest, ReliabilityReport
from .base import BaseAgent

_SYSTEM = """\
You are a site reliability engineer. You assess whether a feature is ready for
production: service level objectives, observability, failure modes, a runbook,
and a validated rollback. You only mark production_ready when operational risks
are addressed. Always respond with a single JSON object and nothing else."""


class SREAgent(BaseAgent):
    """Produces a :class:`ReliabilityReport` for a feature."""

    role = "sre"
    stage = "reliability"
    system_prompt = _SYSTEM

    async def assess(
        self,
        request: FeatureRequest,
        design: Design,
    ) -> ReliabilityReport:
        """Assess production readiness for ``request`` given ``design``."""

        stack = ", ".join(design.tech_stack) or "unspecified"
        prompt = f"""\
Assess production readiness for this feature.

Title: {request.title}
Design overview: {design.overview}
Tech stack: {stack}

Respond with JSON of the form:
{{
  "production_ready": true,
  "summary": "readiness verdict",
  "slos": ["..."],
  "risks": ["..."],
  "runbook": ["..."]
}}"""
        data = await self.ask_json(prompt)
        return parsing.reliability_from_dict(data)
