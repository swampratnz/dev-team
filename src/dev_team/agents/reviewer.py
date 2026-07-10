"""The reviewer agent: approves or requests changes on an implementation."""

from __future__ import annotations

from .. import parsing
from ..models import Implementation, Review, Task
from .base import BaseAgent

_SYSTEM = """\
You are a meticulous code reviewer. You judge whether an implementation meets
the task's acceptance criteria and follows good engineering practice. You only
approve work that is correct and complete. Flag issues with a severity of
info, minor, major, or critical. Any major or critical issue blocks approval.
Always respond with a single JSON object and nothing else."""


class ReviewerAgent(BaseAgent):
    """Produces a :class:`Review` for an implementation."""

    role = "reviewer"
    stage = "review"
    system_prompt = _SYSTEM

    async def review(self, task: Task, implementation: Implementation) -> Review:
        """Review ``implementation`` against ``task``."""

        criteria = "\n".join(
            f"- {c}" for c in task.acceptance_criteria
        ) or "- (none specified)"
        files = "\n".join(
            f"- {f.change_type.value} {f.path}: {f.summary}"
            for f in implementation.files
        ) or "- (no files reported)"
        prompt = f"""\
Review this implementation.

Task {task.id}: {task.title}
Acceptance criteria:
{criteria}

Implementation summary: {implementation.summary}
Files changed:
{files}
Engineer notes: {implementation.notes or "(none)"}

Respond with JSON of the form:
{{
  "approved": true,
  "summary": "overall verdict",
  "comments": [{{"severity": "major", "path": "src/x.py", "message": "..."}}]
}}"""
        data = await self.ask_json(prompt)
        return parsing.review_from_dict(data)
