"""Tests for the blackboard, decision log, and project memory."""

from __future__ import annotations

from dev_team.execution import InMemoryWorkspace
from dev_team.memory import Blackboard, ProjectMemory


def test_blackboard_key_value():
    bb = Blackboard()
    assert bb.get("x") is None
    assert bb.get("x", "d") == "d"
    bb.put("x", 1)
    assert bb.has("x")
    assert bb.get("x") == 1
    bb.put("y", 2)
    assert bb.keys() == ["x", "y"]


def test_blackboard_artifacts():
    bb = Blackboard()
    bb.post_artifact("plan", "p1", "the plan")
    bb.post_artifact("impl", "T1", "did it")
    bb.post_artifact("impl", "T2", "did more")
    assert len(bb.artifacts) == 3
    assert [a.key for a in bb.artifacts_of_kind("impl")] == ["T1", "T2"]


def test_blackboard_decisions_autonumber():
    bb = Blackboard()
    first = bb.record_decision("Use Python", "context", "decided", "cons")
    second = bb.record_decision("Use pytest", "context", "decided")
    assert first.id == "ADR-001"
    assert second.id == "ADR-002"
    assert second.consequences == ""


def test_blackboard_snapshot():
    bb = Blackboard()
    bb.put("k", "v")
    bb.post_artifact("plan", "p", "s")
    bb.record_decision("t", "c", "d")
    snap = bb.snapshot()
    assert snap["entries"] == {"k": "v"}
    assert snap["artifacts"][0]["kind"] == "plan"
    assert snap["decisions"][0]["id"] == "ADR-001"


def test_project_memory_save_and_load():
    ws = InMemoryWorkspace()
    memory = ProjectMemory(ws)
    assert memory.load() is None  # nothing saved yet
    bb = Blackboard()
    bb.put("feature", "login")
    memory.save(bb)
    loaded = memory.load()
    assert loaded["entries"]["feature"] == "login"


def test_checkpoint_store_roundtrip():
    from dev_team.memory import CheckpointStore, RunCheckpoint

    ws = InMemoryWorkspace()
    store = CheckpointStore(ws)
    # nothing stored -> empty checkpoint for the feature
    assert store.load("F").done_task_ids == []
    store.save(RunCheckpoint(feature_title="F", done_task_ids=["T1", "T2"]))
    loaded = store.load("F")
    assert loaded.feature_title == "F"
    assert loaded.done_task_ids == ["T1", "T2"]
    store.clear("F")
    assert store.load("F").done_task_ids == []


def test_checkpoint_store_ignores_other_feature():
    from dev_team.memory import CheckpointStore, RunCheckpoint

    ws = InMemoryWorkspace()
    store = CheckpointStore(ws)
    store.save(RunCheckpoint(feature_title="other", done_task_ids=["T1"]))
    assert store.load("F").done_task_ids == []


def test_blackboard_seed_decision_ids_continues_numbering():
    bb = Blackboard()
    bb.seed_decision_ids(4)
    assert bb.record_decision("t", "c", "d").id == "ADR-005"
    bb.seed_decision_ids(2)  # seeding never rewinds the sequence
    assert bb.record_decision("t", "c", "d").id == "ADR-006"


def test_checkpoint_store_roundtrips_baseline_and_plan():
    from dev_team.memory import CheckpointStore, RunCheckpoint

    ws = InMemoryWorkspace()
    store = CheckpointStore(ws)
    plan = {"summary": "s", "tasks": []}
    store.save(RunCheckpoint(feature_title="F", baseline_sha="abc123", plan=plan))
    loaded = store.load("F")
    assert loaded.baseline_sha == "abc123"
    assert loaded.plan == plan


def test_checkpoint_store_discards_malformed_fields():
    from dev_team.memory import CheckpointStore, RunCheckpoint

    ws = InMemoryWorkspace()
    store = CheckpointStore(ws)
    store.save(RunCheckpoint(feature_title="F", baseline_sha=None, plan="not-a-dict"))
    loaded = store.load("F")
    assert loaded.baseline_sha is None
    assert loaded.plan is None


def test_checkpoint_store_tolerates_corrupt_file():
    from dev_team.memory import CheckpointStore

    ws = InMemoryWorkspace()
    store = CheckpointStore(ws)
    ws.write_text(store._path_for("F"), "{truncated")
    assert store.load("F").done_task_ids == []


def test_checkpoint_store_tolerates_non_dict_json():
    from dev_team.memory import CheckpointStore

    ws = InMemoryWorkspace()
    store = CheckpointStore(ws)
    ws.write_text(store._path_for("F"), "[1, 2]")
    assert store.load("F").done_task_ids == []


def test_project_memory_merges_runs_and_continues_history():
    ws = InMemoryWorkspace()
    memory = ProjectMemory(ws)

    bb1 = Blackboard()
    bb1.record_decision("first", "c", "x")
    bb1.put("retrospective", ["note1"])
    memory.save(bb1)

    bb2 = Blackboard()
    bb2.seed_decision_ids(1)
    bb2.record_decision("second", "c", "y")
    bb2.put("retrospective", ["note2"])
    memory.save(bb2)

    data = memory.load()
    assert [d["id"] for d in data["decisions"]] == ["ADR-001", "ADR-002"]
    assert data["entries"]["retrospective"] == ["note1", "note2"]
    assert data["runs"] == 2


def test_project_memory_dedupes_decision_ids_across_saves():
    ws = InMemoryWorkspace()
    memory = ProjectMemory(ws)
    bb = Blackboard()
    bb.record_decision("only", "c", "x")
    memory.save(bb)
    memory.save(bb)  # same ADR-001 saved twice
    data = memory.load()
    assert len(data["decisions"]) == 1
    assert data["runs"] == 2


def test_project_memory_tolerates_corrupt_or_non_dict_file():
    ws = InMemoryWorkspace()
    memory = ProjectMemory(ws)
    ws.write_text(memory.path, "{oops")
    assert memory.load() is None
    ws.write_text(memory.path, "[1]")
    assert memory.load() is None
