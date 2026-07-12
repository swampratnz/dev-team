"""Tests for the workspace web dashboard."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from dev_team.backlog import BacklogStore, ItemStatus
from dev_team.conventions import ConventionsProfile, ConventionsStore
from dev_team.dashboard import (
    DASHBOARD_HTML,
    DashboardServer,
    collect_state,
)
from dev_team.eventlog import EventLog
from dev_team.events import AgentEvent
from dev_team.execution import InMemoryWorkspace, LocalWorkspace
from dev_team.memory import Blackboard, DecisionRecord, ProjectMemory
from dev_team.persona import DEFAULT_CAST


def _journal(ws, *events, run="deliver-1"):
    ticks = iter(range(1, len(events) + 1))
    log = EventLog(ws, run=run, clock=lambda: float(next(ticks)))
    for event in events:
        log(event)


# --- state collection --------------------------------------------------------------


def test_collect_state_agent_cards_cover_the_default_cast():
    ws = InMemoryWorkspace()
    _journal(
        ws,
        AgentEvent("engineer", "implement", "building T1", "attempt 1", "Sam"),
        AgentEvent("assessment", "start", "engine event: not an agent card"),
        AgentEvent("librarian", "index", "an unexpected extra role"),
    )
    state = collect_state(ws, clock=lambda: 99.0)
    assert state["generated_at"] == 99.0
    assert state["workspace"] == "(in-memory)"
    cards = {card["role"]: card for card in state["agents"]}
    assert set(DEFAULT_CAST) <= set(cards)
    assert cards["engineer"]["last"]["message"] == "building T1"
    assert cards["engineer"]["name"] == "Sam"
    # idle agents still get a card, named from the default cast
    assert cards["qa"]["last"] is None
    assert cards["qa"]["name"] == DEFAULT_CAST["qa"].name
    # unknown roles are appended rather than dropped
    assert cards["librarian"]["last"]["stage"] == "index"
    assert cards["librarian"]["name"] is None
    # engine-level events go to the feed, not the cards
    assert "assessment" not in cards
    assert state["activity"][0]["role"] == "librarian"  # newest first


def test_collect_state_summarises_runs_newest_first():
    ws = InMemoryWorkspace()
    _journal(ws, AgentEvent("engineer", "implement", "old"), run="deliver-old")
    _journal(ws, AgentEvent("qa", "test", "new"), run="assess-new")
    state = collect_state(ws)
    assert [r["id"] for r in state["runs"]] == ["assess-new", "deliver-old"]
    newest = state["runs"][0]
    assert newest["events"] == 1
    assert newest["last_message"] == "new"


def test_run_summaries_tolerate_missing_timestamps():
    from dev_team.dashboard import _run_summaries

    runs = _run_summaries([{"run": "r1", "message": "no ts"}])
    assert runs[0]["ended"] is None


def test_collect_state_backlog_epics_points_and_orphans():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    backlog = store.load()
    epic = backlog.add_epic("Remediation", "from audit")
    done = backlog.add_story("Pin build chain", "", estimate=3, epic_id=epic.id)
    done.status = ItemStatus.DONE
    blocked = backlog.add_story("Upgrade ORM", "", estimate=8, epic_id=epic.id)
    blocked.status = ItemStatus.BLOCKED
    backlog.add_story("Loose story", "", estimate=2)
    store.save(backlog)

    state = collect_state(ws)["backlog"]
    assert state["present"] is True
    assert state["counts"] == {"todo": 1, "in_progress": 0, "done": 1, "blocked": 1}
    (epic_state,) = state["epics"]
    assert epic_state["points_done"] == 3
    assert epic_state["points_total"] == 11
    assert [s["title"] for s in epic_state["stories"]] == [
        "Pin build chain", "Upgrade ORM",
    ]
    assert [s["title"] for s in state["orphan_stories"]] == ["Loose story"]


def test_collect_state_empty_workspace_is_all_absent():
    state = collect_state(InMemoryWorkspace())
    assert state["backlog"]["present"] is False
    assert state["memory"]["present"] is False
    assert state["conventions"]["present"] is False
    assert state["reports"] == []
    assert state["runs"] == []


def test_collect_state_memory_and_conventions_and_reports(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    board = Blackboard()
    board.put("retrospective", ["review rejected T1 twice"])
    board.decisions.append(
        DecisionRecord(id="ADR-1", title="Use boring tech", context="", decision="")
    )
    ProjectMemory(ws).save(board)
    ConventionsStore(ws).save(
        ConventionsProfile.from_dict(
            {"summary": "PascalCase everywhere", "conventions": [
                {"aspect": "naming", "convention": "PascalCase", "evidence": "a.cs"}
            ]}
        )
    )
    ws.write_text("audit/assessment.md", "# report")
    ws.write_text("sub/audit/deep.md", "# nested report")
    ws.write_text("audit/raw.txt", "not markdown")

    state = collect_state(ws)
    assert state["workspace"] == str(tmp_path)
    assert state["memory"]["present"] is True
    assert state["memory"]["runs"] == 1
    assert state["memory"]["retrospectives"] == ["review rejected T1 twice"]
    assert state["memory"]["decisions"] == [{"id": "ADR-1", "title": "Use boring tech"}]
    assert state["conventions"]["summary"] == "PascalCase everywhere"
    assert state["reports"] == ["audit/assessment.md", "sub/audit/deep.md"]


def test_memory_state_filters_non_dict_decisions():
    ws = InMemoryWorkspace(
        {".dev_team/memory.json": json.dumps(
            {"entries": {}, "decisions": ["not a dict"], "runs": 2}
        )}
    )
    memory = collect_state(ws)["memory"]
    assert memory["runs"] == 2
    assert memory["decisions"] == []
    assert memory["retrospectives"] == []


# --- the HTTP server ----------------------------------------------------------------


@pytest.fixture
def server():
    ws = InMemoryWorkspace(
        {"audit/assessment.md": "# the report\n\nClassification: rebuild"}
    )
    _journal(ws, AgentEvent("engineer", "implement", "building"))
    srv = DashboardServer(ws, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def _get(server, path):
    with urllib.request.urlopen(server.url.rstrip("/") + path, timeout=5) as res:
        return res.headers, res.read().decode()


def test_server_serves_the_page_and_state(server):
    _, page = _get(server, "/")
    assert page == DASHBOARD_HTML
    assert "<title>dev-team dashboard</title>" in page

    headers, body = _get(server, "/api/state")
    assert headers["Content-Type"].startswith("application/json")
    state = json.loads(body)
    assert state["activity"][0]["message"] == "building"


def test_server_serves_known_reports_only(server):
    _, body = _get(server, "/api/report?path=audit/assessment.md")
    assert "Classification: rebuild" in body

    for bad in ("/api/report?path=../../etc/passwd", "/api/report", "/nope"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(server, bad)
        assert excinfo.value.code == 404
        excinfo.value.close()  # HTTPError carries the response socket


def test_server_url_names_host_and_port(server):
    assert server.url.startswith("http://127.0.0.1:")
    assert server.url.endswith("/")
