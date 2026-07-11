"""The QA agent: authors tests and reports on coverage.

In the simulation workflow QA *describes* a test run; in the delivery engine
QA :meth:`~QAAgent.author_tests` — it writes real test files (from the
acceptance criteria and the implementation's actual code) that the executable
gates then run. Pass/fail comes from exit codes, never from self-report.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .. import parsing
from ..models import Implementation, Task, TestReport
from .base import BaseAgent
from .reviewer import render_changed_files

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

    async def author_tests(
        self,
        task: Task,
        implementation: Implementation,
        *,
        file_contents: Optional[Mapping[str, str]] = None,
    ) -> Implementation:
        """Write executable test files for ``implementation``.

        Returns an :class:`Implementation` whose files are the test files to
        materialise into the workspace; the Definition-of-Done gates then run
        them for real.
        """

        criteria = "\n".join(
            f"- {c}" for c in task.acceptance_criteria
        ) or "- (none specified)"
        files = render_changed_files(implementation, file_contents)
        prompt = f"""\
Author automated tests for this implementation. The tests will be written into
the workspace and executed for real, so they must be complete, runnable files
that validate the acceptance criteria against the code shown below.

The bar: your tests must FAIL if this implementation were removed or broken —
they will be checked against the pre-change code, and a suite that still
passes without the implementation is rejected as vacuous. Assert on concrete
behaviour (inputs and outputs), never just that code imports or runs.

Task {task.id}: {task.title}
Acceptance criteria:
{criteria}

Changed files (with content):
{files}

Respond with JSON of the form:
{{
  "summary": "what the tests cover",
  "files": [
    {{"path": "tests/test_x.py", "change_type": "create", "summary": "...", "content": "..."}}
  ],
  "notes": ""
}}"""
        data = await self.ask_json(prompt)
        return parsing.implementation_from_dict(data, task.id)
