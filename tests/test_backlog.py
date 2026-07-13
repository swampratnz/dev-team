"""Tests for the persistent backlog and iteration planning."""

from __future__ import annotations

import json

import pytest

from dev_team.backlog import Backlog, BacklogStore, ItemStatus, validate_dependencies
from dev_team.errors import DependencyCycleError, DevTeamError
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


def test_declined_status_roundtrips():
    bl = Backlog()
    bl.add_story("Won't do")
    bl.stories[0].status = ItemStatus.DECLINED
    data = bl.to_dict()
    assert data["stories"][0]["status"] == "declined"
    restored = Backlog.from_dict(data)
    assert restored.stories[0].status is ItemStatus.DECLINED
    # a declined story is neither ready nor counted as velocity
    assert restored.ready_stories() == []
    assert restored.velocity() == 0


def test_depends_on_and_updated_at_roundtrip_only_when_set():
    bl = Backlog()
    first = bl.add_story("First")
    second = bl.add_story("Second")
    second.depends_on = [first.id]
    second.updated_at = 1234.5
    plain, tracked = bl.to_dict()["stories"]
    # unset board fields are omitted — the pre-board on-disk shape is kept
    assert "depends_on" not in plain
    assert "updated_at" not in plain
    assert tracked["depends_on"] == [first.id]
    assert tracked["updated_at"] == 1234.5
    restored = Backlog.from_dict(bl.to_dict())
    assert restored.stories[0].depends_on == []
    assert restored.stories[0].updated_at is None
    assert restored.stories[1].depends_on == [first.id]
    assert restored.stories[1].updated_at == 1234.5


def test_story_ids_are_not_reused_after_a_delete():
    # Count-based minting would hand "S2" back after this delete, silently
    # re-attaching S2's dependency edges/provenance to an unrelated newcomer.
    bl = _backlog()  # S1..S3
    bl.stories = [s for s in bl.stories if s.id != "S2"]
    assert bl.add_story("Replacement").id == "S4"  # one past the max, not len+1
    bl.stories = [s for s in bl.stories if s.id != "S1"]
    assert bl.add_story("Another").id == "S5"


def test_epic_ids_are_not_reused_after_a_delete():
    bl = Backlog()
    bl.add_epic("One")
    bl.add_epic("Two")
    bl.add_epic("Three")
    bl.epics = [e for e in bl.epics if e.id != "E2"]
    assert bl.add_epic("Four").id == "E4"


def test_id_minting_ignores_non_numeric_suffixes():
    # Hand-edited files can hold odd ids; minting skips them rather than dies.
    bl = Backlog.from_dict(
        {
            "epics": [],
            "stories": [
                {"id": "S2x", "title": "odd"},
                {"id": "X9", "title": "other prefix"},
                {"id": "S7", "title": "numeric"},
            ],
        }
    )
    assert bl.add_story("fresh").id == "S8"


def test_validate_dependencies_accepts_a_clean_graph():
    bl = _backlog()
    bl.stories[1].depends_on = ["S1"]
    bl.stories[2].depends_on = ["S1", "S2"]
    validate_dependencies(bl)  # no exception


def test_validate_dependencies_rejects_unknown_and_self_edges():
    bl = _backlog()
    bl.stories[0].depends_on = ["S99"]
    with pytest.raises(ValueError, match="unknown story 'S99'"):
        validate_dependencies(bl)
    bl.stories[0].depends_on = ["S1"]
    with pytest.raises(ValueError, match="depends on itself"):
        validate_dependencies(bl)


def test_validate_dependencies_rejects_a_cycle():
    bl = _backlog()
    bl.stories[0].depends_on = ["S2"]
    bl.stories[1].depends_on = ["S1"]
    with pytest.raises(DependencyCycleError) as excinfo:
        validate_dependencies(bl)
    assert excinfo.value.task_ids == ["S1", "S2"]  # S3 is not implicated


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


def test_from_dict_ignores_unknown_keys():
    # A backlog.json from a newer build (or a hand-edit / renamed field) can
    # carry keys this build does not know; they must be dropped, not raise
    # TypeError from Story(**payload)/Epic(**payload).
    data = {
        "epics": [{"id": "E1", "title": "Auth", "description": "", "colour": "red"}],
        "stories": [
            {"id": "S1", "title": "Login", "estimate": 2, "status": "todo",
             "owner": "alice"}
        ],
    }
    restored = Backlog.from_dict(data)
    assert restored.epics[0].id == "E1"
    assert restored.stories[0].id == "S1"
    assert restored.stories[0].estimate == 2


def test_store_load_raises_devteamerror_on_corrupt_json():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    ws.write_text(store.path, "{not valid json")
    with pytest.raises(DevTeamError):
        store.load()


def test_store_load_raises_devteamerror_on_missing_required_field():
    # After filtering unknown keys, a required field (title) is absent, so
    # construction fails — surfaced as a typed DevTeamError, not a raw
    # TypeError the CLI would let escape as a traceback.
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    ws.write_text(store.path, json.dumps({"stories": [{"renamed_id": "S1"}]}))
    with pytest.raises(DevTeamError):
        store.load()


def test_store_load_raises_devteamerror_on_bad_status():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    ws.write_text(
        store.path,
        json.dumps({"stories": [{"id": "S1", "title": "Login", "status": "bogus"}]}),
    )
    with pytest.raises(DevTeamError):
        store.load()


def test_store_load_raises_devteamerror_on_non_object_json():
    # Valid JSON of the wrong shape (a list at top level): ``.get`` on it
    # raises AttributeError, which must also become a typed DevTeamError.
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    ws.write_text(store.path, json.dumps([1, 2, 3]))
    with pytest.raises(DevTeamError):
        store.load()
