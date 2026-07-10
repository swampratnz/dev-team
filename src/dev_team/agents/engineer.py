"""The engineer agent: implements a task, optionally addressing feedback."""

from __future__ import annotations

from typing import Optional

from .. import parsing
from ..models import Design, Implementation, Review, Task
from .base import BaseAgent

_SYSTEM = """\
You are a senior software engineer. You implement one task at a time, writing
clean, well-structured code. You describe every file you create or modify.
When given review feedback you address every blocking comment.
Always respond with a single JSON object and nothing else."""


def _feedback_section(review: Optional[Review]) -> str:
    """Render prior review feedback for inclusion in the prompt."""

    if review is None:
        return "This is the first attempt; there is no prior feedback."
    lines = [f"- [{c.severity.value}] {c.message}" for c in review.comments]
    body = "\n".join(lines) if lines else "- (no specific comments)"
    return (
        "A previous attempt was rejected in review. Address this feedback:\n"
        f"Reviewer summary: {review.summary}\n{body}"
    )


class EngineerAgent(BaseAgent):
    """Produces an :class:`Implementation` for a task."""

    role = "engineer"
    stage = "implementation"
    system_prompt = _SYSTEM

    async def implement(
        self,
        task: Task,
        design: Design,
        feedback: Optional[Review] = None,
    ) -> Implementation:
        """Implement ``task`` within the context of ``design``."""

        criteria = "\n".join(
            f"- {c}" for c in task.acceptance_criteria
        ) or "- (none specified)"
        prompt = f"""\
Implement the following task.

Task {task.id}: {task.title}
Description:
{task.description}

Acceptance criteria:
{criteria}

Design overview:
{design.overview}

{_feedback_section(feedback)}

Respond with JSON of the form:
{{
  "summary": "what you built",
  "files": [
    {{"path": "src/x.py", "change_type": "create", "summary": "...", "content": "..."}}
  ],
  "notes": "anything reviewers should know"
}}"""
        data = await self.ask_json(prompt)
        return parsing.implementation_from_dict(data, task.id)
