"""The security engineer agent: threat modelling and a security review gate.

Follows the neurosymbolic recipe that dominates the field (IRIS, AIxCC,
SastBench): deterministic scanners find candidates, the model *triages* them
and hunts for what scanners can't see — and every blocking finding must cite
concrete evidence in the code it was shown. Unevidenced vibes don't block
releases.
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

Evidence discipline (false positives erode trust and block good releases):
- Every major or critical finding must cite the file and the specific code
  that is vulnerable, and describe a concrete attack path.
- When scanner output is provided, triage it: confirm real findings with
  evidence, explicitly dismiss noise, and add only what scanners cannot see
  (logic flaws, authz gaps, trust-boundary mistakes).
- If you cannot point at vulnerable code you were shown, the finding is at
  most informational.
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
        scanner_output: Optional[str] = None,
    ) -> SecurityReport:
        """Security-review ``implementation`` for ``task``.

        ``scanner_output`` is the raw output of a SAST/dependency scanner run
        over the workspace; the agent triages it rather than working blind.
        """

        files = render_changed_files(implementation, file_contents)
        scan = (
            "\nSecurity scanner output (triage these findings):\n"
            f"{scanner_output[:6000]}\n"
            if scanner_output
            else "\n(no scanner output available — rely on the code alone)\n"
        )
        prompt = f"""\
Perform a security review of this change.

Task {task.id}: {task.title}
Implementation summary: {implementation.summary}

Changed files (with content):
{files}
{scan}
Respond with JSON of the form:
{{
  "approved": true,
  "summary": "overall security verdict",
  "findings": [
    {{"severity": "major", "category": "injection", "description": "file + code + attack path", "remediation": "..."}}
  ]
}}"""
        data = await self.ask_json(prompt)
        return parsing.security_report_from_dict(data)
