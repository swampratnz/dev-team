"""Tests for the opt-in agent I/O transcript recorder and read helpers."""

from __future__ import annotations

import json

from dev_team.execution import InMemoryWorkspace
from dev_team.sdk import AgentResult
from dev_team.transcripts import (
    TRANSCRIPTS_DIR,
    TranscriptRecorder,
    list_transcripts,
    read_transcript,
)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def _recorder(ws, run="deliver-1", **kwargs):
    return TranscriptRecorder(ws, run=run, clock=_Clock(), **kwargs)


# --- recorder ----------------------------------------------------------------


def test_record_writes_the_expected_file_and_fields():
    ws = InMemoryWorkspace()
    rec = _recorder(ws)
    rec.record(
        role="engineer",
        system_prompt="you are an engineer",
        prompt="build the thing",
        result=AgentResult(text="done", cost_usd=0.25, is_error=False),
    )
    path = f"{TRANSCRIPTS_DIR}/deliver-1/engineer-001.json"
    assert path in ws.list_files()
    data = json.loads(ws.read_text(path))
    assert data == {
        "ts": 1.0,
        "run": "deliver-1",
        "role": "engineer",
        "seq": 1,
        "system_prompt": "you are an engineer",
        "prompt": "build the thing",
        "response": "done",
        "cost_usd": 0.25,
        "is_error": False,
    }


def test_record_assigns_a_per_role_sequence():
    ws = InMemoryWorkspace()
    rec = _recorder(ws)
    for _ in range(2):
        rec.record(role="engineer", system_prompt=None, prompt="p",
                   result=AgentResult(text="x"))
    rec.record(role="qa", system_prompt=None, prompt="p",
               result=AgentResult(text="x"))
    files = ws.list_files()
    assert f"{TRANSCRIPTS_DIR}/deliver-1/engineer-001.json" in files
    assert f"{TRANSCRIPTS_DIR}/deliver-1/engineer-002.json" in files
    assert f"{TRANSCRIPTS_DIR}/deliver-1/qa-001.json" in files


def test_record_truncates_oversized_fields_and_keeps_none():
    ws = InMemoryWorkspace()
    rec = _recorder(ws, max_chars=10)
    rec.record(
        role="engineer",
        system_prompt=None,  # a None field survives as null, never truncated
        prompt="short",  # under the cap, kept verbatim
        result=AgentResult(text="x" * 25),  # over the cap, truncated
    )
    data = json.loads(ws.read_text(f"{TRANSCRIPTS_DIR}/deliver-1/engineer-001.json"))
    assert data["system_prompt"] is None
    assert data["prompt"] == "short"
    assert data["response"] == "x" * 10 + " …[truncated 15 chars]"


# --- list_transcripts --------------------------------------------------------


def test_list_transcripts_returns_sorted_metadata():
    ws = InMemoryWorkspace()
    rec = _recorder(ws)
    rec.record(role="engineer", system_prompt="s", prompt="first prompt " + "a" * 200,
               result=AgentResult(text="r1", cost_usd=0.1))
    rec.record(role="engineer", system_prompt="s", prompt="second",
               result=AgentResult(text="r2", cost_usd=0.2, is_error=True))
    meta = list_transcripts(ws, "deliver-1", "engineer")
    assert [m["seq"] for m in meta] == [1, 2]
    assert meta[0]["cost_usd"] == 0.1
    assert meta[1]["is_error"] is True
    # the preview is capped and drawn from the prompt
    assert meta[0]["prompt_preview"].startswith("first prompt ")
    assert len(meta[0]["prompt_preview"]) <= 140
    assert set(meta[0]) == {"seq", "ts", "cost_usd", "is_error", "prompt_preview"}


def test_list_transcripts_empty_when_none_recorded():
    assert list_transcripts(InMemoryWorkspace(), "deliver-1", "engineer") == []


def test_list_transcripts_ignores_other_files_and_corrupt_records():
    ws = InMemoryWorkspace()
    _recorder(ws).record(role="engineer", system_prompt="s", prompt="ok",
                         result=AgentResult(text="r"))
    # a non-transcript file, a corrupt json, and a non-dict json all under the
    # role prefix must be skipped without raising.
    ws.write_text("src/app.py", "print(1)")
    ws.write_text(f"{TRANSCRIPTS_DIR}/deliver-1/engineer-corrupt.json", "{not json")
    ws.write_text(f"{TRANSCRIPTS_DIR}/deliver-1/engineer-list.json", "[1, 2, 3]")
    meta = list_transcripts(ws, "deliver-1", "engineer")
    assert [m["seq"] for m in meta] == [1]


def test_list_transcripts_tolerates_a_non_int_seq_when_sorting():
    ws = InMemoryWorkspace()
    ws.write_text(
        f"{TRANSCRIPTS_DIR}/deliver-1/engineer-009.json",
        json.dumps({"seq": "oops", "ts": 1.0, "cost_usd": 0, "is_error": False,
                    "prompt": "p"}),
    )
    _recorder(ws).record(role="engineer", system_prompt="s", prompt="ok",
                         result=AgentResult(text="r"))
    meta = list_transcripts(ws, "deliver-1", "engineer")
    # the malformed seq sorts as 0; the real record (seq 1) follows it
    assert [m["seq"] for m in meta] == ["oops", 1]


def test_list_transcripts_rejects_crafted_run_and_role():
    ws = InMemoryWorkspace()
    _recorder(ws).record(role="engineer", system_prompt="s", prompt="ok",
                         result=AgentResult(text="r"))
    assert list_transcripts(ws, "../etc", "engineer") == []
    assert list_transcripts(ws, "deliver-1", "../../secret") == []
    assert list_transcripts(ws, "deliver/1", "engineer") == []
    assert list_transcripts(ws, "", "engineer") == []
    assert list_transcripts(ws, "deliver-1", 123) == []  # non-string role
    # a token with a non-filename char (space) is rejected by the fullmatch
    assert list_transcripts(ws, "deliver 1", "engineer") == []


# --- read_transcript ---------------------------------------------------------


def test_read_transcript_returns_the_full_record():
    ws = InMemoryWorkspace()
    _recorder(ws).record(role="engineer", system_prompt="sys", prompt="p",
                        result=AgentResult(text="resp", cost_usd=0.3))
    record = read_transcript(ws, "deliver-1", "engineer", 1)
    assert record["response"] == "resp"
    assert record["system_prompt"] == "sys"
    assert record["cost_usd"] == 0.3


def test_read_transcript_accepts_a_string_seq():
    ws = InMemoryWorkspace()
    _recorder(ws).record(role="engineer", system_prompt="sys", prompt="p",
                        result=AgentResult(text="resp"))
    assert read_transcript(ws, "deliver-1", "engineer", "1")["seq"] == 1


def test_read_transcript_none_for_unknown_or_guarded():
    ws = InMemoryWorkspace()
    _recorder(ws).record(role="engineer", system_prompt="sys", prompt="p",
                        result=AgentResult(text="resp"))
    # a real but absent seq
    assert read_transcript(ws, "deliver-1", "engineer", 99) is None
    # crafted traversal / bad inputs are rejected before any path is built
    assert read_transcript(ws, "../etc", "engineer", 1) is None
    assert read_transcript(ws, "deliver-1", "..", 1) is None
    assert read_transcript(ws, "deliver-1", "engineer", "1x") is None
    assert read_transcript(ws, "deliver-1", "engineer", "..") is None


def test_read_transcript_none_for_corrupt_or_non_dict_member():
    ws = InMemoryWorkspace()
    ws.write_text(f"{TRANSCRIPTS_DIR}/deliver-1/engineer-001.json", "{not json")
    ws.write_text(f"{TRANSCRIPTS_DIR}/deliver-1/engineer-002.json", "42")
    assert read_transcript(ws, "deliver-1", "engineer", 1) is None
    assert read_transcript(ws, "deliver-1", "engineer", 2) is None
