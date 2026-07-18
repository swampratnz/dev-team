"""Tests for the pure backlog-foreman selection logic (ROADMAP #9)."""

from __future__ import annotations

from dev_team.backlog import Backlog, ItemStatus, Story
from dev_team.foreman import (
    DEFAULT_MAX_STORIES,
    MAX_STORIES_CEILING,
    ready_for_delivery,
    story_job_description,
)


def _backlog(*stories):
    backlog = Backlog()
    backlog.stories.extend(stories)
    return backlog


def test_bounds_are_sane():
    assert 1 <= DEFAULT_MAX_STORIES <= MAX_STORIES_CEILING


def test_ready_selects_todo_stories_in_backlog_order():
    backlog = _backlog(
        Story(id="S1", title="a"),
        Story(id="S2", title="b", status=ItemStatus.DONE),
        Story(id="S3", title="c"),
    )
    assert [s.id for s in ready_for_delivery(backlog)] == ["S1", "S3"]


def test_ready_excludes_in_progress_blocked_and_declined():
    backlog = _backlog(
        Story(id="S1", title="a", status=ItemStatus.IN_PROGRESS),
        Story(id="S2", title="b", status=ItemStatus.BLOCKED),
        Story(id="S3", title="c", status=ItemStatus.DECLINED),
    )
    assert ready_for_delivery(backlog) == []


def test_ready_gates_on_unfinished_dependencies():
    backlog = _backlog(
        Story(id="S1", title="a"),
        Story(id="S2", title="b", depends_on=["S1"]),
    )
    # S1 is TODO, so S2 waits; S1 itself is ready
    assert [s.id for s in ready_for_delivery(backlog)] == ["S1"]


def test_done_and_declined_dependencies_both_satisfy():
    backlog = _backlog(
        Story(id="S1", title="a", status=ItemStatus.DONE),
        Story(id="S2", title="b", status=ItemStatus.DECLINED),
        Story(id="S3", title="c", depends_on=["S1", "S2"]),
    )
    assert [s.id for s in ready_for_delivery(backlog)] == ["S3"]


def test_in_progress_or_blocked_dependency_does_not_satisfy():
    for status in (ItemStatus.IN_PROGRESS, ItemStatus.BLOCKED):
        backlog = _backlog(
            Story(id="S1", title="a", status=status),
            Story(id="S2", title="b", depends_on=["S1"]),
        )
        assert ready_for_delivery(backlog) == []


def test_empty_backlog_is_empty_plan():
    assert ready_for_delivery(Backlog()) == []


def test_story_job_description_prefers_the_story_description():
    story = Story(id="S1", title="Fix auth", description="rotate the secret")
    assert story_job_description(story) == "rotate the secret"


def test_story_job_description_falls_back_deterministically():
    story = Story(id="S7", title="Fix auth")
    assert story_job_description(story) == "Implement backlog story S7: Fix auth"
