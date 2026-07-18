"""The backlog foreman: turn dependency-ready stories into deliver jobs.

The second half of ROADMAP #9 (top-level orchestration). Assessments breed a
dependency-ordered remediation backlog, but nothing decides *what to work on
next* — a human curls one deliver job at a time. The foreman closes that gap
**deterministically**: no model picks work. Selection is pure code over the
persisted backlog — `TODO` stories whose every dependency is finished, in
backlog order — and each selected story becomes one bounded deliver job on the
dispatch service's existing single-flight queue, tagged with the story id so
its outcome writes the story's status back (``in_progress`` → ``done`` /
``blocked``; one attempt per story, never auto-retried — a ``blocked`` story
waits for a human, exactly like ``needs-human``).

This module holds the pure parts (selection, sizing, the job brief); the
dispatch service owns the stateful parts (submitting, provenance, write-back)
under its existing locks. See ``docs/DISPATCH.md``.
"""

from __future__ import annotations

from typing import List

from .backlog import Backlog, ItemStatus, Story

#: How many stories one ``POST /foreman/run`` may enqueue when the request
#: does not say — deliberately small, echoing the development pipeline's own
#: WIP cap, so a full backlog never floods the queue (or the budget) at once.
DEFAULT_MAX_STORIES = 3

#: Hard ceiling on ``max_stories`` — a bigger batch must be several explicit
#: runs, not one request. Requests above it are rejected, not clamped: this
#: knob multiplies spend, so an out-of-range value is a mistake to surface,
#: never to silently shrink.
MAX_STORIES_CEILING = 10

#: Statuses that satisfy a dependency edge: the work landed, or a human
#: explicitly declined it (a declined dependency must not wedge its dependents
#: forever — the human already decided it will never be done).
_SATISFIED = (ItemStatus.DONE, ItemStatus.DECLINED)


def ready_for_delivery(backlog: Backlog) -> List[Story]:
    """The ``TODO`` stories whose every dependency is finished, in backlog order.

    Backlog order is deterministic and already dependency-shaped (assessment
    plan stories are pre-chained in order), so no model — and no clock — is
    consulted. ``in_progress`` and ``blocked`` stories are never re-selected:
    ``in_progress`` is already running as a job, and ``blocked`` waits for a
    human, so one story gets exactly one autonomous attempt.
    """

    satisfied = {s.id for s in backlog.stories if s.status in _SATISFIED}
    return [
        story
        for story in backlog.stories
        if story.status is ItemStatus.TODO
        and all(dep in satisfied for dep in story.depends_on)
    ]


def story_job_description(story: Story) -> str:
    """The deliver job's description for ``story`` (never blank).

    A deliver job requires a non-blank description — the manual ``/jobs``
    submit path enforces ``description.strip()`` — and a hand-written card may
    carry none (or only whitespace, which is truthy and would slip past a
    plain ``or``), so fall back to a deterministic one naming the story.
    """

    if story.description.strip():
        return story.description
    return f"Implement backlog story {story.id}: {story.title}"
