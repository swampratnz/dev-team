"""The technical writer agent: user docs, API reference, release notes."""

from __future__ import annotations

from .. import parsing
from ..models import Design, Documentation, FeatureRequest
from .base import BaseAgent

_SYSTEM = """\
You are a technical writer. You produce clear, accurate documentation for a
delivered feature: an overview, usage/API notes, and release notes. Write for
real users. Always respond with a single JSON object and nothing else."""


class TechnicalWriterAgent(BaseAgent):
    """Produces :class:`Documentation` for a feature."""

    role = "technical-writer"
    stage = "documentation"
    system_prompt = _SYSTEM

    async def document(
        self,
        request: FeatureRequest,
        design: Design,
    ) -> Documentation:
        """Write documentation for ``request`` given its ``design``."""

        prompt = f"""\
Write documentation for this delivered feature.

Title: {request.title}
Description:
{request.description}
Design overview: {design.overview}

Respond with JSON of the form:
{{
  "summary": "what the docs cover",
  "sections": [{{"title": "Overview", "content": "..."}}]
}}"""
        data = await self.ask_json(prompt)
        return parsing.documentation_from_dict(data)
