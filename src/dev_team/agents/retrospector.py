"""The retrospective agent: mines a finished run's evidence for root causes.

The engine already distils a *deterministic* retrospective (which tasks failed,
which needed multiple attempts, whether security blocked). That is reliable but
shallow — it reports *what* happened, never *why*. This agent reads the run's
evidence (the trace summary, the scorecard, and each task's outcome) and returns
a few specific, forward-looking lessons that name a cause and a change, so the
next run's plan can avoid the same cost. It is opt-in and runs after delivery,
so it never gates a successful run.
"""

from __future__ import annotations

from typing import List

from .. import parsing
from ..fences import defuse
from ..models import Design, FeatureRequest
from .base import UNTRUSTED_CONTENT_NOTE, BaseAgent

_SYSTEM = """\
You are a delivery retrospective analyst. You are given the evidence from a
software delivery run that has already finished — a trace summary, the run
scorecard, and each task's outcome. Find the ROOT CAUSES behind what went wrong
or was hard, and turn them into a few specific, forward-looking lessons the team
can act on next time.

A good lesson names a cause and a change: not "task T3 failed" (the evidence
already says that) but "T3 took three attempts because the design left the
error-handling contract implicit — plans for parsing work should add an explicit
acceptance criterion for malformed input". Prefer causes that link a design or
planning choice to a downstream cost (attempts, review rejections, gate
failures). If the run went cleanly, say so in a single lesson rather than
inventing problems. Always respond with a single JSON object and nothing else."""

#: Cap the number of lessons and each lesson's length so one verbose reflection
#: cannot flood the persisted retrospective (which re-enters the next run's
#: planning prompt).
MAX_LESSONS = 5
_LESSON_CHARS = 300


class RetrospectorAgent(BaseAgent):
    """Produces a short list of root-cause lessons for a finished run."""

    role = "retrospective"
    stage = "retrospective"
    system_prompt = _SYSTEM + UNTRUSTED_CONTENT_NOTE

    async def reflect(
        self,
        request: FeatureRequest,
        design: Design,
        run_evidence: str,
    ) -> List[str]:
        """Return root-cause lessons distilled from ``run_evidence``.

        ``run_evidence`` is a compact, engine-built digest of the run (task
        outcomes, scorecard, trace summary), fenced as untrusted ``<evidence>``
        because it embeds model-origin text (task titles, review summaries).
        """

        prompt = f"""\
Analyse this finished delivery run and return the root-cause lessons.

Feature: {request.title}
Design overview: {design.overview}

<evidence>
{defuse(run_evidence, 'evidence')}
</evidence>

Respond with JSON of the form:
{{
  "lessons": ["a specific, forward-looking lesson naming a cause and a change"]
}}"""
        data = await self.ask_json(prompt)
        lessons = parsing.as_str_list(parsing.as_dict(data), "lessons")
        cleaned: List[str] = []
        for lesson in lessons:
            text = " ".join(lesson.split())
            if not text:
                continue
            if len(text) > _LESSON_CHARS:
                text = text[:_LESSON_CHARS].rstrip() + "..."
            cleaned.append(text)
            if len(cleaned) >= MAX_LESSONS:
                break
        return cleaned
