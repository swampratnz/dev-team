"""Tests for the workspace web dashboard."""

from __future__ import annotations

import http.client
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from dev_team.backlog import BacklogStore, ItemStatus
from dev_team.conventions import ConventionsProfile, ConventionsStore
from dev_team.dashboard import (
    DASHBOARD_HTML,
    LOGIN_HTML,
    DashboardServer,
    agent_history,
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
    assert state["counts"] == {
        "todo": 1, "in_progress": 0, "done": 1, "blocked": 1, "declined": 0,
    }
    (epic_state,) = state["epics"]
    assert epic_state["points_done"] == 3
    assert epic_state["points_total"] == 11
    assert [s["title"] for s in epic_state["stories"]] == [
        "Pin build chain", "Upgrade ORM",
    ]
    assert [s["title"] for s in state["orphan_stories"]] == ["Loose story"]


def test_collect_state_backlog_carries_story_detail_and_provenance():
    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    backlog = store.load()
    epic = backlog.add_epic(
        "Remediation — acme/rota", "From assessment of acme/rota"
    )
    backlog.add_story(
        "Remove hardcoded secret: connection string",
        "Evidence: Web.config line 12",
        estimate=1,
        epic_id=epic.id,
        source_job="assess-1",
        finding_id="risk.secrets[0]",
    )
    backlog.add_story("Patch Moq 4.2: GHSA-1", "https://osv.dev/GHSA-1", epic_id=epic.id)
    store.save(backlog)

    state = collect_state(ws)["backlog"]
    (epic_state,) = state["epics"]
    # the epic's description reaches the page (the repo/classification line)
    assert epic_state["description"] == "From assessment of acme/rota"
    llm, deterministic = epic_state["stories"]
    # the story modal needs the full description plus the re-verify hook ids
    assert llm["description"] == "Evidence: Web.config line 12"
    assert llm["source_job"] == "assess-1"
    assert llm["finding_id"] == "risk.secrets[0]"
    # deterministic stories surface None so the page shows the muted note
    assert deterministic["source_job"] is None
    assert deterministic["finding_id"] is None


def test_collect_state_backlog_carries_board_fields():
    """The Kanban board's fields reach the state payload.

    Every story dict now carries ``depends_on`` (the dependency indicator /
    blocked-by flag) and ``updated_at``; the counts dict gains ``declined``.
    """

    ws = InMemoryWorkspace()
    store = BacklogStore(ws)
    backlog = store.load()
    epic = backlog.add_epic("Remediation — acme/rota")
    first = backlog.add_story("Land the migration", "", estimate=3, epic_id=epic.id)
    second = backlog.add_story("Cut over reads", "", estimate=2, epic_id=epic.id)
    second.depends_on = [first.id]
    second.updated_at = 1700000000.0
    declined = backlog.add_story("Rewrite in Rust", "", epic_id=epic.id)
    declined.status = ItemStatus.DECLINED
    store.save(backlog)

    state = collect_state(ws)["backlog"]
    assert state["counts"] == {
        "todo": 2, "in_progress": 0, "done": 0, "blocked": 0, "declined": 1,
    }
    (epic_state,) = state["epics"]
    by_id = {s["id"]: s for s in epic_state["stories"]}
    assert by_id[second.id]["depends_on"] == [first.id]
    assert by_id[second.id]["updated_at"] == 1700000000.0
    assert by_id[first.id]["depends_on"] == []
    assert by_id[first.id]["updated_at"] is None
    assert by_id[declined.id]["status"] == "declined"


def test_dashboard_page_kanban_board_desk_check():
    """Static desk-check of the Kanban board JS (CI has no browser).

    The interactive board rides on the PR-A ``/api/backlog/*`` proxy; the
    load-bearing properties pinned here: the four columns plus a declined
    section render with counts, every mutation route is called through the
    single ``backlogWrite`` helper, ids are URL-encoded, every repo-derived
    card field (titles, dependency titles, echoed input values) flows
    through ``esc()`` before innerHTML, and error messages surface via
    ``textContent`` only.
    """

    # the four columns, in board order, plus the muted declined row
    assert '["todo", "To do"], ["in_progress", "In progress"]' in DASHBOARD_HTML
    assert '["blocked", "Blocked"], ["done", "Done"]' in DASHBOARD_HTML
    assert "Declined (${declined.length})" in DASHBOARD_HTML
    assert '<span class="cn">${items.length}</span>' in DASHBOARD_HTML  # counts
    # the declined chip and stat tile
    assert "declined: chip(" in DASHBOARD_HTML
    assert '"stories declined"' in DASHBOARD_HTML
    # every write goes through the one same-origin proxy helper
    assert '"/api/backlog/" + path' in DASHBOARD_HTML
    assert '"Content-Type": "application/json"' in DASHBOARD_HTML
    # ... and each PR-A mutation route is reachable from a control
    assert '"/status", { status }' in DASHBOARD_HTML                    # move
    assert '+ "/decline"' in DASHBOARD_HTML                              # decline
    assert '+ "/deps"' in DASHBOARD_HTML                                 # dependency editor
    assert 'backlogWrite("POST", "story", payload)' in DASHBOARD_HTML    # add card
    assert 'backlogWrite("PATCH", "story/"' in DASHBOARD_HTML            # edit
    assert 'backlogWrite("DELETE", "story/"' in DASHBOARD_HTML           # delete
    assert "encodeURIComponent(id)" in DASHBOARD_HTML
    # the reliable move path is a <select> on every card (and in the modal)
    assert 'select class="cmove" data-move="${esc(st.id)}"' in DASHBOARD_HTML
    assert "moveStory(sel.dataset.move, sel.value" in DASHBOARD_HTML
    # dependency indicator + blocked-by flag, titles resolved and ESCAPED
    assert "\\u26D3 ${deps.length}" in DASHBOARD_HTML
    assert "blocked by unfinished ${esc(unfinished[0].title)}" in DASHBOARD_HTML
    assert 'd.status !== "done" && d.status !== "declined"' in DASHBOARD_HTML
    # escape-first card rendering (titles clamped via .ct)
    assert '<div class="ct">${esc(st.title)}</div>' in DASHBOARD_HTML
    # add card per epic; edit form values escaped even though user-typed
    # (they round-trip through the server before rendering)
    assert 'data-add="${esc(e.id)}"' in DASHBOARD_HTML
    assert "\\uFF0B Add card" in DASHBOARD_HTML
    assert 'value="${esc(st.title)}"' in DASHBOARD_HTML
    assert '>${esc(st.description ?? "")}</textarea>' in DASHBOARD_HTML
    # the dependency editor never offers the card itself
    assert "o.id !== st.id" in DASHBOARD_HTML
    # delete requires a confirm step
    assert "confirm delete?" in DASHBOARD_HTML
    # dispatch error messages reach the DOM via textContent, never innerHTML
    assert "el.textContent = msg; // SECURITY: textContent, never innerHTML" in DASHBOARD_HTML
    assert "if (data && data.error) msg = String(data.error);" in DASHBOARD_HTML


def test_dashboard_page_story_modal_desk_check():
    """Static desk-check of the story-modal JS (CI has no browser).

    Stories are repo-derived (assessment findings quote repository content),
    so the load-bearing properties are pinned against the page source: rows
    are keyboard-operable buttons, every story field flows through esc()
    before innerHTML (a <script> in a description must render inert), the
    dev_team_verify one-liner appears only when BOTH provenance ids exist,
    and deterministic stories get the muted non-verifiable note.
    """

    # clickable, keyboard-operable story rows opening the modal
    assert 'role="button" tabindex="0" data-story="${esc(st.id)}"' in DASHBOARD_HTML
    assert "openStory(row.dataset.story)" in DASHBOARD_HTML
    assert 'e.key === "Enter" || e.key === " "' in DASHBOARD_HTML
    # escape-first rendering of the untrusted fields shown in the modal
    assert "${st.description ? esc(st.description)" in DASHBOARD_HTML
    assert "${esc(st.epic)}" in DASHBOARD_HTML
    assert "${esc(st.source_job)}" in DASHBOARD_HTML
    # the title bypasses innerHTML entirely (textContent never parses HTML)
    assert '$("story-title").textContent = st.id' in DASHBOARD_HTML
    # the re-verify hook requires BOTH ids and is escaped end to end
    assert "if (st.finding_id && st.source_job)" in DASHBOARD_HTML
    assert '"dev_team_verify " + st.source_job + " " + st.finding_id' in DASHBOARD_HTML
    assert "<code>${esc(cmd)}</code>" in DASHBOARD_HTML
    assert 'data-copy="${esc(cmd)}"' in DASHBOARD_HTML
    # deterministic stories: the muted, non-verifiable note
    assert "Deterministic finding (dependency/dead-code scan)" in DASHBOARD_HTML
    assert "not agent-verifiable" in DASHBOARD_HTML
    # modal chrome reuses the shared overlay machinery (close / Esc / outside)
    assert 'id="story-overlay"' in DASHBOARD_HTML
    assert 'id="story-close"' in DASHBOARD_HTML
    assert "closeStory(); }" in DASHBOARD_HTML
    assert "<title>dev-team dashboard</title>" in DASHBOARD_HTML


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


# --- agent history -----------------------------------------------------------------


def test_agent_history_filters_by_role_oldest_first():
    ws = InMemoryWorkspace()
    _journal(
        ws,
        AgentEvent("engineer", "implement", "first", "attempt 1", "Sam"),
        AgentEvent("qa", "test", "someone else's event"),
        AgentEvent("engineer", "review", "second"),
        run="deliver-1",
    )
    history = agent_history(ws, "engineer")
    assert [h["message"] for h in history] == ["first", "second"]
    # only the timeline fields survive, oldest first
    assert history[0] == {
        "ts": 1.0,
        "run": "deliver-1",
        "stage": "implement",
        "message": "first",
        "detail": "attempt 1",
    }


def test_agent_history_groups_multiple_runs_in_order():
    ws = InMemoryWorkspace()
    _journal(ws, AgentEvent("engineer", "implement", "old"), run="deliver-old")
    _journal(ws, AgentEvent("engineer", "review", "new"), run="deliver-new")
    assert [h["run"] for h in agent_history(ws, "engineer")] == [
        "deliver-old",
        "deliver-new",
    ]


def test_agent_history_caps_at_the_last_hundred():
    ws = InMemoryWorkspace()
    _journal(
        ws,
        *[AgentEvent("engineer", "step", f"m{i}") for i in range(120)],
        run="big",
    )
    history = agent_history(ws, "engineer")
    assert len(history) == 100
    # the newest survive; oldest-first means m119 is last
    assert history[0]["message"] == "m20"
    assert history[-1]["message"] == "m119"


def test_agent_history_empty_for_unknown_or_absent_role():
    ws = InMemoryWorkspace()
    _journal(ws, AgentEvent("engineer", "implement", "building"))
    assert agent_history(ws, "nobody") == []
    assert agent_history(ws, "") == []


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


def test_server_serves_agent_history_json(server):
    headers, body = _get(server, "/api/agent?role=engineer")
    assert headers["Content-Type"].startswith("application/json")
    data = json.loads(body)
    assert data["role"] == "engineer"
    assert data["name"] == DEFAULT_CAST["engineer"].name
    assert [h["message"] for h in data["history"]] == ["building"]
    assert set(data["history"][0]) == {"ts", "run", "stage", "message", "detail"}


def test_server_agent_history_unknown_and_absent_role(server):
    # An unknown role is a 200 with an empty timeline, never an error.
    _, body = _get(server, "/api/agent?role=ghost")
    assert json.loads(body) == {"role": "ghost", "name": "ghost", "history": []}
    # A missing role parameter degrades to the empty case too.
    _, body = _get(server, "/api/agent")
    assert json.loads(body) == {"role": "", "name": "", "history": []}


def test_server_url_names_host_and_port(server):
    assert server.url.startswith("http://127.0.0.1:")
    assert server.url.endswith("/")


# --- transcripts routes ------------------------------------------------------


@pytest.fixture
def transcript_server():
    from dev_team.sdk import AgentResult
    from dev_team.transcripts import TranscriptRecorder

    ws = InMemoryWorkspace()
    _journal(ws, AgentEvent("engineer", "implement", "building"), run="deliver-1")
    rec = TranscriptRecorder(ws, run="deliver-1", clock=lambda: 5.0)
    rec.record(role="engineer", system_prompt="be an engineer", prompt="build it",
               result=AgentResult(text="<script>alert(1)</script>", cost_usd=0.2))
    srv = DashboardServer(ws, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def test_transcripts_list_route(transcript_server):
    headers, body = _get(transcript_server, "/api/transcripts?run=deliver-1&role=engineer")
    assert headers["Content-Type"].startswith("application/json")
    data = json.loads(body)
    assert data["run"] == "deliver-1"
    assert data["role"] == "engineer"
    assert [t["seq"] for t in data["transcripts"]] == [1]
    assert data["transcripts"][0]["cost_usd"] == 0.2


def test_transcripts_list_empty_is_still_200(transcript_server):
    _, body = _get(transcript_server, "/api/transcripts?run=deliver-1&role=ghost")
    assert json.loads(body)["transcripts"] == []


def test_transcript_detail_route(transcript_server):
    _, body = _get(transcript_server, "/api/transcript?run=deliver-1&role=engineer&seq=1")
    data = json.loads(body)
    assert data["system_prompt"] == "be an engineer"
    # raw, unescaped in the JSON payload; the client escapes it before the DOM
    assert data["response"] == "<script>alert(1)</script>"


def test_transcript_detail_unknown_or_guarded_is_404(transcript_server):
    for bad in (
        "/api/transcript?run=deliver-1&role=engineer&seq=99",   # absent seq
        "/api/transcript?run=../etc&role=engineer&seq=1",        # traversal run
        "/api/transcript?run=deliver-1&role=..&seq=1",           # traversal role
        "/api/transcript?run=deliver-1&role=engineer&seq=x",     # bad seq
        "/api/transcript",                                        # no params
    ):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(transcript_server, bad)
        assert excinfo.value.code == 404
        excinfo.value.close()


# --- token auth (opt-in stopgap) ---------------------------------------------

TOKEN = "sekrit-dash-token"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def token_server():
    ws = InMemoryWorkspace(
        {"audit/assessment.md": "# the report\n\nClassification: rebuild"}
    )
    _journal(ws, AgentEvent("engineer", "implement", "building"))
    srv = DashboardServer(ws, port=0, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


def _request(server, method, path, *, headers=None, body=None):
    """One raw request; unlike urllib it never follows the 303 redirects."""

    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        return res.status, dict(res.getheaders()), res.read().decode()
    finally:
        conn.close()


def test_token_server_401s_every_route_without_auth(token_server):
    # page routes get the login form (a browser can render it) ...
    for page in ("/", "/nope"):
        status, headers, body = _request(token_server, "GET", page)
        assert status == 401
        assert headers["Content-Type"].startswith("text/html")
        assert body == LOGIN_HTML
        assert "<title>dev-team login</title>" in body
        assert TOKEN not in body
    # ... API routes get bare JSON, never HTML
    for api in (
        "/api/state",
        "/api/report?path=audit/assessment.md",
        "/api/agent?role=engineer",
        "/api/transcripts?run=deliver-1&role=engineer",
        "/api/transcript?run=deliver-1&role=engineer&seq=1",
    ):
        status, headers, body = _request(token_server, "GET", api)
        assert status == 401
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body) == {"error": "unauthorized"}


def test_token_server_accepts_the_bearer_header(token_server):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    status, _, body = _request(token_server, "GET", "/", headers=auth)
    assert status == 200
    assert "<title>dev-team dashboard</title>" in body
    status, _, body = _request(token_server, "GET", "/api/state", headers=auth)
    assert status == 200
    assert json.loads(body)["activity"][0]["message"] == "building"


def test_token_server_accepts_the_session_cookie(token_server):
    cookie = {"Cookie": f"devteam_dash={TOKEN}"}
    status, _, body = _request(token_server, "GET", "/api/state", headers=cookie)
    assert status == 200
    assert json.loads(body)["activity"][0]["message"] == "building"


def test_token_server_rejects_bad_credentials(token_server):
    for headers in (
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"bearer {TOKEN}"},        # exact scheme, like dispatch
        {"Authorization": "Bearer wrongÿ"},     # non-ASCII → 401, never a 500
        {"Cookie": "devteam_dash=wrong"},
        {"Cookie": f"other={TOKEN}"},                # right value, wrong cookie
        {"Cookie": "not a cookie;; ="},              # malformed header
    ):
        status, _, _ = _request(token_server, "GET", "/api/state", headers=headers)
        assert status == 401


def test_login_correct_token_sets_cookie_and_redirects(token_server):
    status, headers, _ = _request(
        token_server, "POST", "/login", body=f"token={TOKEN}", headers=FORM
    )
    assert status == 303
    assert headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert f"devteam_dash={TOKEN}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    # the browser replays the cookie and is in
    status, _, body = _request(
        token_server, "GET", "/", headers={"Cookie": f"devteam_dash={TOKEN}"}
    )
    assert status == 200
    assert "<title>dev-team dashboard</title>" in body


def test_login_wrong_token_shows_the_form_again(token_server):
    status, headers, body = _request(
        token_server, "POST", "/login", body="token=wrong", headers=FORM
    )
    assert status == 401
    assert "Set-Cookie" not in headers
    assert "<title>dev-team login</title>" in body
    assert "Invalid token." in body
    assert TOKEN not in body  # nothing about the expected value leaks


def test_login_bodies_are_bounded_and_validated(token_server):
    # no body at all (Content-Length: 0)
    status, _, body = _request(token_server, "POST", "/login")
    assert status == 401
    assert "Invalid token." in body
    # a malformed Content-Length reads as no body
    status, _, _ = _request(
        token_server, "POST", "/login", headers={"Content-Length": "xyz"}
    )
    assert status == 401
    # an oversized body is rejected without being read
    status, _, _ = _request(
        token_server, "POST", "/login",
        body="token=" + "x" * 5000, headers=FORM,
    )
    assert status == 401
    # a well-formed body without a token field fails closed
    status, _, _ = _request(
        token_server, "POST", "/login", body="user=me", headers=FORM
    )
    assert status == 401


def test_logout_clears_the_cookie(token_server):
    status, headers, _ = _request(
        token_server, "POST", "/logout",
        headers={"Cookie": f"devteam_dash={TOKEN}"},
    )
    assert status == 303
    assert headers["Location"] == "/"
    cookie = headers["Set-Cookie"]
    assert "devteam_dash=;" in cookie
    assert "Max-Age=0" in cookie


def test_post_routing_respects_auth(token_server):
    # unknown POSTs are gated exactly like GETs ...
    status, _, body = _request(token_server, "POST", "/api/state")
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}
    status, _, body = _request(token_server, "POST", "/nope")
    assert status == 401
    assert "<title>dev-team login</title>" in body
    # ... and with credentials an unknown POST is a plain 404
    status, _, _ = _request(
        token_server, "POST", "/nope",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 404


def test_open_server_login_lifecycle_is_harmless(server):
    # No token configured: /login grants nothing (no cookie), /logout still
    # clears, unknown POSTs 404, and every GET stays open — exact back-compat.
    status, headers, _ = _request(server, "POST", "/login")
    assert status == 303
    assert headers["Location"] == "/"
    assert "Set-Cookie" not in headers
    status, headers, _ = _request(server, "POST", "/logout")
    assert status == 303
    assert "Max-Age=0" in headers["Set-Cookie"]
    status, _, _ = _request(server, "POST", "/nope")
    assert status == 404


# --- the board write proxy (/api/backlog/* → the dispatch service) ------------

DISPATCH_URL = "http://dispatch.test:8738"
DISPATCH_TOKEN = "sekrit-dispatch-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def proxy_server():
    ws = InMemoryWorkspace()
    srv = DashboardServer(
        ws, port=0, token=TOKEN,
        dispatch_url=DISPATCH_URL, dispatch_token=DISPATCH_TOKEN,
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join(timeout=5)


class _FakeDispatchResponse:
    """The slice of urlopen's response the proxy uses (a context manager)."""

    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_urlopen(monkeypatch, *, status=200, body=b'{"ok": true}', error=None):
    """Swap urlopen for a fake that records the outbound Request."""

    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        if error is not None:
            raise error
        return _FakeDispatchResponse(status, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_proxy_forwards_post_with_the_dispatch_bearer(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch, status=201, body=b'{"id": "S9", "title": "Runbook"}'
    )
    payload = json.dumps({"title": "Runbook"})
    status, headers, body = _request(
        proxy_server, "POST", "/api/backlog/story",
        headers={**AUTH, "Content-Type": "application/json"}, body=payload,
    )
    # the dispatch answer (status + body) is relayed verbatim
    assert status == 201
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"id": "S9", "title": "Runbook"}
    # the outbound request: right URL, method, auth, content type, and body
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/backlog/story"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    assert request.data == payload.encode()


def test_proxy_forwards_patch_and_delete_methods(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch, body=b'{"id": "S1"}')
    status, _, _ = _request(
        proxy_server, "PATCH", "/api/backlog/story/S1",
        headers=AUTH, body=json.dumps({"estimate": 2}),
    )
    assert status == 200
    status, _, _ = _request(proxy_server, "DELETE", "/api/backlog/story/S1",
                            headers=AUTH)
    assert status == 200
    patch, delete = seen
    assert patch.get_method() == "PATCH"
    assert patch.full_url == f"{DISPATCH_URL}/backlog/story/S1"
    assert json.loads(patch.data) == {"estimate": 2}
    assert delete.get_method() == "DELETE"
    assert delete.data is None  # no body → none forwarded
    assert delete.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"


def test_proxy_relays_a_dispatch_rejection(proxy_server, monkeypatch):
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/backlog/story/S1/deps", 400, "Bad Request", None,
        io.BytesIO(b'{"error": "story S1 depends on itself"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, headers, body = _request(
        proxy_server, "POST", "/api/backlog/story/S1/deps",
        headers=AUTH, body=json.dumps({"depends_on": ["S1"]}),
    )
    assert status == 400
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "story S1 depends on itself"}


def test_proxy_unreachable_dispatch_is_502(proxy_server, monkeypatch):
    _capture_urlopen(monkeypatch, error=urllib.error.URLError("refused"))
    status, _, body = _request(
        proxy_server, "POST", "/api/backlog/story", headers=AUTH, body="{}"
    )
    assert status == 502
    assert json.loads(body) == {"error": "dispatch service unreachable"}
    assert "refused" not in body  # no internals leak


def test_proxy_tolerates_a_malformed_content_length(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    status, _, _ = _request(
        proxy_server, "POST", "/api/backlog/story/S1/decline",
        headers={**AUTH, "Content-Length": "xyz"},
    )
    assert status == 200
    (request,) = seen
    assert request.data is None  # unreadable length → treated as no body


def test_proxy_requires_dashboard_auth_first(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    for method in ("POST", "PATCH", "DELETE"):
        status, headers, body = _request(
            proxy_server, method, "/api/backlog/story/S1"
        )
        assert status == 401
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body) == {"error": "unauthorized"}
    # a PATCH/DELETE outside /api/ gets the login page, like GET/POST do
    status, headers, _ = _request(proxy_server, "PATCH", "/nope")
    assert status == 401
    assert headers["Content-Type"].startswith("text/html")
    assert seen == []  # nothing was ever forwarded


def test_proxy_scope_is_strictly_api_backlog(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    # authorised writes anywhere else are 404 — the proxy is not a general
    # passthrough to the dispatch service
    for method, path in (
        ("POST", "/api/state"),
        ("POST", "/jobs"),
        ("PATCH", "/api/report"),
        ("DELETE", "/api/transcripts"),
        ("POST", "/api/backlogs/story"),  # prefix must match exactly
    ):
        status, _, _ = _request(proxy_server, method, path, headers=AUTH)
        assert status == 404
    assert seen == []


def test_proxy_unconfigured_board_editing_is_501(token_server, monkeypatch):
    # token_server has no dispatch_url/dispatch_token wired
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        token_server, "POST", "/api/backlog/story", headers=AUTH, body="{}"
    )
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "board editing not configured"}
    assert seen == []


def test_proxy_url_without_token_stays_read_only(monkeypatch):
    # A dispatch URL alone (e.g. localhost dev with no DEV_TEAM_DISPATCH_TOKEN)
    # must not forward unauthenticated writes: still 501.
    seen = _capture_urlopen(monkeypatch)
    srv = DashboardServer(
        InMemoryWorkspace(), port=0, token=TOKEN, dispatch_url=DISPATCH_URL
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(
            srv, "DELETE", "/api/backlog/story/S1", headers=AUTH
        )
        assert status == 501
        assert json.loads(body) == {"error": "board editing not configured"}
        assert seen == []
    finally:
        srv.shutdown()
        thread.join(timeout=5)
