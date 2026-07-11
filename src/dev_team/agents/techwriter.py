"""The technical writer agent: user docs, API reference, release notes.

Docs are a shipped artifact, not a report: the writer sees the actual changed
code and produces documentation *files* that are written into the workspace
and committed with the feature. Claims must be grounded in the code shown —
documentation that describes imagined behaviour is worse than none.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple

from .. import parsing
from ..models import Design, Documentation, FeatureRequest, Implementation
from .base import BaseAgent
from .reviewer import render_changed_files

_SYSTEM = """\
You are a technical writer. You produce clear, accurate documentation for a
delivered feature: an overview, usage/API notes, and release notes. Write for
real users. Every claim and every code example must be grounded in the actual
code you are shown — never document behaviour you cannot point to. Prefer
updating existing documentation files over creating parallel ones.
Always respond with a single JSON object and nothing else."""


class TechnicalWriterAgent(BaseAgent):
    """Produces :class:`Documentation` plus doc files for a feature."""

    role = "technical-writer"
    stage = "documentation"
    system_prompt = _SYSTEM

    async def write_docs(
        self,
        request: FeatureRequest,
        design: Design,
        implementation: Implementation,
        *,
        file_contents: Optional[Mapping[str, str]] = None,
        existing_docs: Optional[Sequence[str]] = None,
    ) -> Tuple[Documentation, Implementation]:
        """Write documentation files for the delivered change.

        Returns the structured :class:`Documentation` summary plus an
        :class:`Implementation` whose files are the actual documentation to
        materialise into the workspace (e.g. a README section, CHANGELOG
        entry, or docs page).
        """

        files = render_changed_files(implementation, file_contents)
        docs_listing = "\n".join(f"- {d}" for d in (existing_docs or [])) or "- (none)"
        prompt = f"""\
Write documentation for this delivered feature, grounded in the actual code
below.

Title: {request.title}
Description:
{request.description}
Design overview: {design.overview}

Delivered code (document what this actually does):
{files}

Existing documentation files in the workspace:
{docs_listing}

Respond with JSON of the form:
{{
  "summary": "what the docs cover",
  "sections": [{{"title": "Overview", "content": "..."}}],
  "files": [
    {{"path": "docs/<feature>.md", "change_type": "create", "summary": "...", "content": "full file content"}}
  ]
}}"""
        data = await self.ask_json(prompt)
        documentation = parsing.documentation_from_dict(data)
        doc_files = parsing.implementation_from_dict(data, "DOCS")
        return documentation, doc_files
