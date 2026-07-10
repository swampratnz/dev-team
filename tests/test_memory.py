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
