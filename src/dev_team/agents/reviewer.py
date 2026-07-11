"""The reviewer agent: approves or requests changes on an implementation.

The review is evidence-based: the prompt carries the actual content of the
changed files (or the diff), not just the engineer's own summary of them.
"""

from __future__ import annotations

from typing import Mapping, Optional

from .. import parsing
from ..models import Implementation, Review, Task
from .base import BaseAgent

_SYSTEM = """\
You are a meticulous code reviewer. You judge whether an implementation meets
the task's acceptance criteria and follows good engineering practice, based on
the actual code you are shown. You only approve work that is correct and
complete. Flag issues with a severity of info, minor, major, or critical. Any
major or critical issue blocks approval.

Review discipline (a good review is precise, not prolific):
- Raise at most {budget} comments; spend them on the issues that matter most.
- Every comment must be actionable: it names the file and states the concrete
  change a developer would make. Never restate what static analysis already
  reported unless you are escalating its severity with a reason.
- No style nitpicks a formatter or linter would catch; no vague advice.
Always respond with a single JSON object and nothing else."""

# The comment budget: production review systems (Google, Meta, GitHub) all
# optimize precision at a fixed comment count, not comment volume.
COMMENT_BUDGET = 6

# Caps keep prompts bounded on large changes; truncation is labelled so the
# reviewer knows it saw a prefix, not the whole file.
PER_FILE_CHARS = 6_000
TOTAL_CHARS = 30_000
DIFF_CHARS = 20_000


def render_diff(diff: Optional[str], *, limit: int = DIFF_CHARS) -> str:
    """Render a git diff for the prompt, truncated with a visible marker."""

    if not diff:
        return ""
    body = diff[:limit]
    if len(body) < len(diff):
        body += "\n... (diff truncated)"
    return f"\nGit diff of the change (what actually changed):\n{body}\n"


def render_changed_files(
    implementation: Implementation,
    contents: Optional[Mapping[str, str]] = None,
    *,
    per_file_chars: int = PER_FILE_CHARS,
    total_chars: int = TOTAL_CHARS,
) -> str:
    """Render the changed files, with real content where available."""

    if not implementation.files:
        return "- (no files reported)"
    lines = []
    budget = total_chars
    for change in implementation.files:
        lines.append(f"--- {change.change_type.value} {change.path}: {change.summary}")
        body = (contents or {}).get(change.path, change.content)
        if not body or budget <= 0:
            continue
        snippet = body[:per_file_chars]
        if len(snippet) > budget:
            snippet = snippet[:budget]
        if len(snippet) < len(body):
            snippet += "\n... (truncated)"
        budget -= len(snippet)
        lines.append(snippet)
    return "\n".join(lines)


class ReviewerAgent(BaseAgent):
    """Produces a :class:`Review` for an implementation."""

    role = "reviewer"
    stage = "review"
    system_prompt = _SYSTEM.format(budget=COMMENT_BUDGET)

    async def review(
        self,
        task: Task,
        implementation: Implementation,
        *,
        file_contents: Optional[Mapping[str, str]] = None,
        diff: Optional[str] = None,
        static_findings: Optional[str] = None,
    ) -> Review:
        """Review ``implementation`` against ``task``.

        ``file_contents`` maps changed paths to their current (post-apply)
        content, so the reviewer judges what is actually in the workspace.
        ``diff`` (when git is available) shows precisely what changed, which
        matters when a modified file is large. ``static_findings`` is linter/
        type-checker output — the reviewer triages it rather than re-deriving
        it, and spends its own judgment on what tools cannot see.
        """

        criteria = "\n".join(
            f"- {c}" for c in task.acceptance_criteria
        ) or "- (none specified)"
        files = render_changed_files(implementation, file_contents)
        analysis = (
            "\nStatic analysis output (triage: escalate what matters, ignore noise):\n"
            f"{static_findings[:4000]}\n"
            if static_findings
            else ""
        )
        prompt = f"""\
Review this implementation.

Task {task.id}: {task.title}
Acceptance criteria:
{criteria}

Implementation summary: {implementation.summary}
Engineer notes: {implementation.notes or "(none)"}

Changed files (with content):
{files}
{render_diff(diff)}{analysis}
Respond with JSON of the form:
{{
  "approved": true,
  "summary": "overall verdict",
  "comments": [{{"severity": "major", "path": "src/x.py", "message": "..."}}]
}}"""
        data = await self.ask_json(prompt)
        return parsing.review_from_dict(data)
