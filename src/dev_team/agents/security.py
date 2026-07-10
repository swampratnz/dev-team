"""The security engineer agent: threat modelling and a security review gate.

Like the code reviewer, the security review is evidence-based: the prompt
carries the actual content of the changed files, not just their names.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .. import parsing
from ..models import Implementation, SecurityReport, Task
from .base import BaseAgent
from .reviewer import render_changed_files

_SYSTEM = """\
You are an application security engineer. You threat-model an implementation and
review its actual code for vulnerabilities: injection, authn/authz flaws,
secrets handling, unsafe dependencies, SSRF, path traversal, and insecure
defaults. You classify each finding as info, minor, major, or critical; any
major or critical finding blocks release.
Always respond with a single JSON object and nothing else."""


class SecurityEngineerAgent(BaseAgent):
    """Produces a :class:`SecurityReport` for an implementation."""

    role = "security-engineer"
    stage = "security-review"
    system_prompt = _SYSTEM

    async def review(
        self,
        task: Task,
        implementation: Implementation,
        *,
        file_contents: Optional[Mapping[str, str]] = None,
    ) -> SecurityReport:
        """Security-review ``implementation`` for ``task``."""

        files = render_changed_files(implementation, file_contents)
        prompt = f"""\
Perform a security review of this change.

Task {task.id}: {task.title}
Implementation summary: {implementation.summary}

Changed files (with content):
{files}

Respond with JSON of the form:
{{
  "approved": true,
  "summary": "overall security verdict",
  "findings": [
    {{"severity": "major", "category": "injection", "description": "...", "remediation": "..."}}
  ]
}}"""
        data = await self.ask_json(prompt)
        return parsing.security_report_from_dict(data)
