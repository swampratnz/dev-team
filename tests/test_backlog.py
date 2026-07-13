"""Tests for the persistent backlog and iteration planning."""

from __future__ import annotations

import pytest

from dev_team.backlog import Backlog, BacklogStore, ItemStatus
from dev_team.execution import InMemoryWorkspace


def _backlog():
    bl = Backlog()
    epic = bl.add_epic("Auth", "authentication")
    bl.add_story("Login", "email/pw", estimate=3, epic_id=epic.id)
    bl.add_story("Logout", "end session", estimate=1, epic_id=epic.id)
    bl.add_story("Reset", "reset pw", estimate=5, epic_id=epic.id)
    return bl


def test_add_and_query():
    bl = _backlog()
    assert bl.epics[0].id == "E1"
    assert [s.id for s in bl.stories] == ["S1", "S2", "S3"]
    assert len(bl.stories_for_epic("E1")) == 3
    assert len(bl.ready_stories()) == 3


def test_add_story_rejects_bad_estimate():
    with pytest.raises(ValueError):
        Backlog().add_story("x", estimate=0)


def test_plan_iteration_respects_capacity():
    bl = _backlog()
    # Capacity 4 fits S1(3) and S2(1) but not S3(5).
    iteration = bl.plan_iteration(1, capacity=4)
    assert [s.id for s in iteration.stories] == ["S1", "S2"]
    assert iteration.committed_points == 4


def test_plan_iteration_rejects_negative_capacity():
    with pytest.raises(ValueError):
        _backlog().plan_iteration(1, capacity=-1)


def test_velocity_counts_done():
    bl = _backlog()
    bl.stories[0].status = ItemStatus.DONE
    bl.stories[1].status = ItemStatus.DONE
    assert bl.velocity() == 4
    # a done story is no longer "ready"
    assert [s.id for s in bl.ready_stories()] == ["S3"]


def test_roundtrip_serialisation():
    bl = _backlog()
    bl.stories[0].status = ItemStatus.DONE
    restored = Backlog.from_dict(bl.to_dict())
    assert [e.id for e in restored.epics] == ["E1"]
    assert restored.stories[0].status is ItemStatus.DONE
    assert restored.velocity() == 3


def test_store_save_and_load():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    assert store.load().stories == []  # empty when nothing saved
    store.save(_backlog())
    loaded = store.load()
    assert len(loaded.stories) == 3


def test_story_provenance_fields_roundtrip():
    bl = Backlog()
    epic = bl.add_epic("Remediation — acme/rota", "from audit")
    bl.add_story(
        "Remove hardcoded secret",
        "Evidence: Web.config",
        estimate=1,
        epic_id=epic.id,
        source_job="assess-1",
        finding_id="risk.secrets[0]",
    )
    bl.add_story("Plain story", "no provenance")
    data = bl.to_dict()
    tracked, plain = data["stories"]
    assert tracked["source_job"] == "assess-1"
    assert tracked["finding_id"] == "risk.secrets[0]"
    # unset provenance is omitted — the pre-provenance on-disk shape is kept
    assert "source_job" not in plain
    assert "finding_id" not in plain
    restored = Backlog.from_dict(data)
    assert restored.stories[0].source_job == "assess-1"
    assert restored.stories[0].finding_id == "risk.secrets[0]"
    assert restored.stories[1].source_job is None
    assert restored.stories[1].finding_id is None


def test_backlog_json_written_before_provenance_still_loads():
    # A backlog.json persisted by an older version has no provenance keys.
    data = {
        "epics": [{"id": "E1", "title": "Auth", "description": ""}],
        "stories": [
            {"id": "S1", "title": "Login", "description": "", "estimate": 1,
             "status": "todo", "epic_id": "E1"}
        ],
    }
    restored = Backlog.from_dict(data)
    assert restored.stories[0].source_job is None
    assert restored.stories[0].finding_id is None
