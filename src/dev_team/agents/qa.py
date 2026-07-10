"""The QA agent: authors tests and reports on coverage."""

from __future__ import annotations

from .. import parsing
from ..models import Implementation, Task, TestReport
from .base import BaseAgent

_SYSTEM = """\
You are a quality assurance engineer. You design unit, integration, and
end-to-end tests for an implementation and report whether they pass and what
line coverage they achieve. Be honest: only report passed=true when the tests
genuinely validate the acceptance criteria.
Always respond with a single JSON object and nothing else."""


class QAAgent(BaseAgent):
    """Produces a :class:`TestReport` for an implementation."""

    role = "qa"
    stage = "testing"
    system_prompt = _SYSTEM

    async def test(self, task: Task, implementation: Implementation) -> TestReport:
        """Author and evaluate tests for ``implementation``."""

        criteria = "\n".join(
            f"- {c}" for c in task.acceptance_criteria
        ) or "- (none specified)"
        prompt = f"""\
Design and evaluate tests for this implementation.

Task {task.id}: {task.title}
Acceptance criteria:
{criteria}
Implementation summary: {implementation.summary}

Respond with JSON of the form:
{{
  "passed": true,
  "coverage": 100.0,
  "summary": "test outcome",
  "cases": [{{"name": "...", "kind": "unit", "target": "..."}}]
}}"""
        data = await self.ask_json(prompt)
        return parsing.test_report_from_dict(data)
