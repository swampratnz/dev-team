"""The architect agent: produces a technical design with tradeoff analysis.

Grounded in what the research says separates good LLM architecture work from
noise: consistency with prior decisions (feeding 3–5 recent ADRs beats model
scale — arXiv 2604.03826), explicit alternatives-and-tradeoffs (ATAM-style),
and an anti-pattern self-check (R2ABench scores designs partly by anti-pattern
detection).
"""

from __future__ import annotations

from typing import Optional, Sequence

from .. import parsing
from ..models import Design, FeatureRequest, Plan
from .base import UNTRUSTED_CONTENT_NOTE, BaseAgent

_SYSTEM = """\
You are a pragmatic software architect. You turn a plan into a concise technical
design: the components involved, their responsibilities, the technology choices,
and the key risks. You always weigh at least one alternative approach and say
why the chosen one wins. You stay consistent with the project's prior
architecture decisions unless you explicitly supersede one with justification.
Before answering you self-check the design for common anti-patterns (god
component, circular dependencies, needless indirection, distributed monolith)
and fix any you find. Always respond with a single JSON object and nothing
else."""

# Feeding a handful of recent decisions is the highest-leverage context for
# design consistency; more than ~5 dilutes rather than helps.
MAX_PRIOR_DECISIONS = 5


class ArchitectAgent(BaseAgent):
    """Produces a :class:`Design` for a request and its plan."""

    role = "architect"
    stage = "design"
    system_prompt = _SYSTEM + UNTRUSTED_CONTENT_NOTE

    async def design(
        self,
        request: FeatureRequest,
        plan: Plan,
        *,
        repo_context: Optional[str] = None,
        relevant_code: Optional[str] = None,
        prior_decisions: Optional[Sequence[str]] = None,
    ) -> Design:
        """Produce a technical design for ``request`` given ``plan``.

        ``repo_context`` describes the existing codebase (its file tree and
        manifest heads) so the design extends what is actually there;
        ``relevant_code`` is the retrieved body of the files most relevant to
        the feature (already fenced as untrusted ``<file-content>`` blocks), so
        the design fits the real code rather than just its names;
        ``prior_decisions`` are the team's recent ADRs (most recent last) that
        this design must stay consistent with.
        """

        task_lines = "\n".join(
            f"- {task.id}: {task.title}" for task in plan.tasks
        ) or "- (no tasks)"
        existing = (
            "\nExisting codebase (design must fit into it):\n"
            f"<repo-context>\n{repo_context}\n</repo-context>\n"
            if repo_context
            else ""
        )
        relevant = (
            f"\nMost relevant existing code:\n{relevant_code}\n"
            if relevant_code
            else ""
        )
        decisions = ""
        if prior_decisions:
            recent = list(prior_decisions)[-MAX_PRIOR_DECISIONS:]
            lines = "\n".join(f"- {d}" for d in recent)
            decisions = (
                "\nPrior architecture decisions (stay consistent, or explicitly "
                f"supersede with justification):\n{lines}\n"
            )
        prompt = f"""\
Design the technical solution for this feature.

Title: {request.title}
Description:
{request.description}

Planned tasks:
{task_lines}
{existing}{relevant}{decisions}
Consider at least one alternative approach and state the tradeoff that decided
against it. Self-check for anti-patterns before answering.

Respond with JSON of the form:
{{
  "overview": "high level approach",
  "components": [{{"name": "...", "responsibility": "..."}}],
  "tech_stack": ["..."],
  "risks": ["..."],
  "alternatives": ["rejected option — the tradeoff that ruled it out"],
  "rationale": "why the chosen approach wins"
}}"""
        data = await self.ask_json(prompt)
        return parsing.design_from_dict(data)
