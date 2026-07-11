"""The engineer agent: implements a task, optionally addressing feedback.

Two modes:

- :meth:`EngineerAgent.implement` — *described* mode: the engineer returns the
  full content of every file as JSON and the caller materialises it. Suitable
  for dry runs and in-memory workspaces.
- :meth:`EngineerAgent.implement_in_place` — *agentic* mode: the engineer gets
  real tools (read/write/edit/run) and a working directory, does the work
  itself — reading existing code before changing it, writing tests, running
  them — and reports a summary. This is the mode that can work on an existing
  codebase.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .. import parsing
from ..models import Design, Implementation, Review, Task
from .base import BaseAgent

_SYSTEM = """\
You are a senior software engineer. You implement one task at a time, writing
clean, well-structured code with automated tests for the acceptance criteria.
You read existing code before modifying it. When given review feedback you
address every blocking comment.
Always respond with a single JSON object and nothing else."""

# The tool loop that makes the engineer a real agent rather than a text
# generator. These are auto-permitted by the SDK when passed as allowed_tools.
TOOLS: Sequence[str] = ("Read", "Write", "Edit", "Bash", "Grep", "Glob")


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


# Cap the listing so a large repo cannot flood the prompt.
MAX_LISTING_ENTRIES = 200


def _listing_section(listing: Optional[Sequence[str]]) -> str:
    """Render the current workspace contents for inclusion in the prompt."""

    if not listing:
        return "The workspace is currently empty."
    shown = list(listing[:MAX_LISTING_ENTRIES])
    files = "\n".join(f"- {path}" for path in shown)
    remainder = len(listing) - len(shown)
    if remainder > 0:
        files += f"\n- ... and {remainder} more file(s)"
    return f"Files currently in the workspace:\n{files}"


def _task_section(task: Task, design: Design) -> str:
    criteria = "\n".join(
        f"- {c}" for c in task.acceptance_criteria
    ) or "- (none specified)"
    return f"""\
Task {task.id}: {task.title}
Description:
{task.description}

Acceptance criteria:
{criteria}

Design overview:
{design.overview}"""


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
        *,
        workspace_listing: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
    ) -> Implementation:
        """Implement ``task`` by describing every file change as JSON."""

        prompt = f"""\
Implement the following task.

{_task_section(task, design)}

{_listing_section(workspace_listing)}

{_feedback_section(feedback)}

Include automated tests for the acceptance criteria among the files you write.
For a "modify" change you must supply the complete new content of the file.

Respond with JSON of the form:
{{
  "summary": "what you built",
  "files": [
    {{"path": "src/x.py", "change_type": "create", "summary": "...", "content": "..."}}
  ],
  "notes": "anything reviewers should know"
}}"""
        data = await self.ask_json(prompt, model=model)
        return parsing.implementation_from_dict(data, task.id)

    async def implement_in_place(
        self,
        task: Task,
        design: Design,
        feedback: Optional[Review] = None,
        *,
        cwd: str,
        model: Optional[str] = None,
        tools: Optional[Sequence[str]] = None,
    ) -> Implementation:
        """Implement ``task`` directly in ``cwd`` using real tools.

        The engineer reads the existing code, makes the changes, writes tests,
        and runs them itself; the returned JSON lists what changed (paths and
        summaries — the workspace itself is the source of truth for content).
        """

        prompt = f"""\
Implement the following task in the current working directory. Use your tools:
read the existing code before changing it, create or edit files directly,
write automated tests for the acceptance criteria, and run the test suite to
check your work before answering.

{_task_section(task, design)}

{_feedback_section(feedback)}

When the work is complete, respond with JSON of the form (no file contents —
the files on disk are the deliverable):
{{
  "summary": "what you built",
  "files": [
    {{"path": "src/x.py", "change_type": "create", "summary": "..."}}
  ],
  "notes": "anything reviewers should know"
}}"""
        data = await self.ask_json(
            prompt, allowed_tools=tools if tools is not None else TOOLS, cwd=cwd, model=model
        )
        return parsing.implementation_from_dict(data, task.id)
