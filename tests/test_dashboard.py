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
from dev_team.scores import RunScore, ScoreHistory


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
    verify curl one-liner appears only when BOTH provenance ids exist,
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
    # the re-verify hook requires BOTH ids and is escaped end to end; the
    # command is the REAL dispatch mode:"verify" submit (POST /jobs, see
    # docs/DISPATCH.md), built from the two repo-derived provenance ids
    assert "if (st.finding_id && st.source_job)" in DASHBOARD_HTML
    assert (
        'JSON.stringify({ mode: "verify", source_job: st.source_job, '
        "finding_id: st.finding_id })"
    ) in DASHBOARD_HTML
    assert (
        "curl -sX POST http://127.0.0.1:8738/jobs "
        '-H "Authorization: Bearer $DEV_TEAM_DISPATCH_TOKEN" '
        "-H \"Content-Type: application/json\" -d '${shBody}'"
    ) in DASHBOARD_HTML
    # the -d body is POSIX single-quote-escaped (defense-in-depth)
    assert "const shBody = body.split(sq).join(sq + bs + sq + sq);" in DASHBOARD_HTML
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


def test_dashboard_html_calibration_panel_escapes_and_handles_empty():
    """Static desk-check of the calibration panel JS (CI has no browser).

    Pins: the panel is wired into the memory column next to House
    conventions, every phase name and count flows through ``esc()`` before
    ``innerHTML`` (a ``<script>``-named phase must render inert), an
    ``overall`` row is always appended, and a zero-total rollup renders the
    muted empty state rather than an empty table.
    """

    assert 'details("calibration", "Verdict calibration"' in DASHBOARD_HTML
    assert "if (!cal.overall.total) return" in DASHBOARD_HTML
    assert "no verifications recorded yet" in DASHBOARD_HTML
    # every phase/overall row field passes through esc()
    assert "<td>${esc(phase)}</td>" in DASHBOARD_HTML
    assert "<td>${esc(b.confirmed)}</td><td>${esc(b.refuted)}</td><td>${esc(b.needs_context)}</td>" in DASHBOARD_HTML
    assert "<td>${esc(b.total)}</td><td>${esc(rate)}</td>" in DASHBOARD_HTML
    assert 'calibrationRow("overall", cal.overall)' in DASHBOARD_HTML


def test_dashboard_html_calibration_summary_renders_report_quality_totals():
    """Static desk-check of the new blind-spot/broken-citation summary line.

    Pins: the summary line's visibility is its own ``blind_spot_total ||
    broken_citation_total`` check (AC6) — independent of ``cal.overall.total``,
    so it must still render when there are zero verifications recorded but
    non-zero report-quality totals; and it must render nothing when both new
    totals are zero, matching the existing zero-count-suppression precedent.
    """

    assert "function calibrationSummary(cal)" in DASHBOARD_HTML
    assert "if (!cal.blind_spot_total && !cal.broken_citation_total) return" in DASHBOARD_HTML
    assert "const summary = calibrationSummary(cal);" in DASHBOARD_HTML
    assert (
        "if (!cal.overall.total) return summary || "
        '\'<span class="muted">no verifications recorded yet</span>\';'
    ) in DASHBOARD_HTML
    assert "return summary + `<table class=\"cal-table\">" in DASHBOARD_HTML


def test_dashboard_html_score_history_wired_into_memory_panel():
    """Static desk-check of the score-history JS (CI has no browser).

    Pins: the block is wired into the memory column next to Verdict
    calibration, gated on ``s.score_history.present`` (so an absent trail
    renders nothing for this block rather than an empty list), and every
    numeric/string field flows through ``esc()`` before ``innerHTML``.
    """

    assert (
        'if (s.score_history.present) {\n'
        '    parts.push(details("score-history", "Score history", scoreHistoryPanel(s.score_history), false));'
    ) in DASHBOARD_HTML
    assert "function scoreHistoryPanel(sh)" in DASHBOARD_HTML
    assert "no delivery runs recorded yet" in DASHBOARD_HTML
    assert "function scoreHistoryRow(r)" in DASHBOARD_HTML
    assert "<li><b>${esc(r.feature)}</b>: ${headline}${delta}</li>" in DASHBOARD_HTML
    assert (
        '`${r.success ? "ok" : "FAILED"}, ${esc(r.tasks_succeeded)}/${esc(r.tasks_total)} tasks, `'
    ) in DASHBOARD_HTML
    assert "${esc(r.total_attempts)} attempt(s), $${esc(r.cost_usd.toFixed(4))}" in DASHBOARD_HTML
    assert 'r.delta ? ` <span class="muted">| delta ${esc(r.delta)}</span>` : ""' in DASHBOARD_HTML


def test_dashboard_html_score_history_feature_is_escaped_before_innerhtml():
    """SECURITY (AC6): a ``RunScore.feature`` value is caller/dispatch-supplied,

    ultimately free-text, untrusted content — the one field in a score-history
    row that isn't a deterministic number. Pins that it renders through
    ``esc()`` (never raw) before reaching ``innerHTML``, so a feature name
    like ``<img src=x onerror=alert(1)>`` renders as inert escaped text
    rather than executing.
    """

    assert "<li><b>${esc(r.feature)}</b>:" in DASHBOARD_HTML
    assert "<b>${r.feature}</b>" not in DASHBOARD_HTML


def test_dashboard_html_report_meta_chips_render_independently():
    """Static desk-check of the Reports panel's blind-spot/broken-citation chips.

    Pins: each metric's chip visibility is its own ``> 0`` condition (not a
    single paired toggle), so a report with only one of the two signals
    renders exactly one chip — never both or neither — and a report with
    neither signal (or absent from ``report_meta`` entirely) renders none,
    matching the existing archived-chip-only behavior unchanged.
    """

    assert "function reportMetaChips(meta)" in DASHBOARD_HTML
    assert "if (meta.blind_spot_count > 0)" in DASHBOARD_HTML
    assert 'chip(meta.blind_spot_count + " blind spots", "idle")' in DASHBOARD_HTML
    assert "if (meta.broken_citation_count > 0)" in DASHBOARD_HTML
    assert 'chip(meta.broken_citation_count + " broken citations", "blocked")' in DASHBOARD_HTML
    assert "const reportMeta = s.report_meta || {};" in DASHBOARD_HTML
    assert "reportMetaChips(jobId ? reportMeta[jobId] : null)" in DASHBOARD_HTML


def test_dashboard_html_report_modal_audit_quality_block_escapes_model_output():
    """Static desk-check of the report modal's "Audit quality" detail block.

    SECURITY (AC7): ``broken_citations`` values are a finding's own claimed
    evidence string a *model* wrote, not a deterministic path like
    ``blind_spots`` — so both must go through ``esc()`` before touching
    ``innerHTML``, matching the access-log panel's precedent for any
    caller/model-influenced text.
    """

    assert "function auditQualityBlock(meta)" in DASHBOARD_HTML
    assert "<li>blind spot: ${esc(dir)}</li>" in DASHBOARD_HTML
    assert (
        "<li>broken citation (${esc(phase)}): ${esc(citation)}</li>"
        in DASHBOARD_HTML
    )
    assert '<div class="audit-quality">' in DASHBOARD_HTML
    assert (
        "$(\"modal-body\").innerHTML = auditQualityBlock(meta) + renderMarkdown(text);"
        in DASHBOARD_HTML
    )
    assert "const meta = jobId && state ? state.report_meta[jobId] : null;" in DASHBOARD_HTML


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


def _run_score(feature="F", *, success=True, attempts=1, cost=0.0, scorecard=None):
    return RunScore(
        feature=feature,
        success=success,
        tasks_total=1,
        tasks_succeeded=1 if success else 0,
        total_attempts=attempts,
        cost_usd=cost,
        committed=success,
        scorecard=scorecard or {},
    )


def test_score_history_state_absent_is_not_present():
    state = collect_state(InMemoryWorkspace())["score_history"]
    assert state == {"present": False, "runs": []}


def test_score_history_state_orders_newest_first_with_deltas():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    hist.record(_run_score("First", attempts=1, cost=0.01))
    hist.record(_run_score("Second", attempts=2, cost=0.03))
    state = collect_state(ws)["score_history"]
    assert state["present"] is True
    assert [r["feature"] for r in state["runs"]] == ["Second", "First"]
    # newest-first: the oldest entry shown has no prior run to diff against
    assert state["runs"][1]["delta"] is None
    # the newer entry's delta matches _score_deltas against the run before it
    assert state["runs"][0]["delta"] == "attempts +1, cost +$0.0200"
    assert state["runs"][0]["success"] is True
    assert state["runs"][0]["tasks_succeeded"] == 1
    assert state["runs"][0]["tasks_total"] == 1
    assert state["runs"][0]["total_attempts"] == 2
    assert state["runs"][0]["cost_usd"] == 0.03


def test_score_history_state_caps_at_newest_eight():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    for i in range(10):
        hist.record(_run_score(f"run-{i}"))
    state = collect_state(ws)["score_history"]
    assert len(state["runs"]) == 8
    # newest 8 (run-2 .. run-9), newest first — run-0/run-1 dropped
    assert [r["feature"] for r in state["runs"]] == [f"run-{i}" for i in range(9, 1, -1)]


def test_score_history_state_no_delta_when_nothing_changed():
    ws = InMemoryWorkspace()
    hist = ScoreHistory(ws)
    hist.record(_run_score("First", attempts=1, cost=0.01))
    hist.record(_run_score("Second", attempts=1, cost=0.01))
    state = collect_state(ws)["score_history"]
    assert state["runs"][0]["delta"] is None


def test_collect_state_score_history_key_matches_score_history_state():
    from dev_team.dashboard import _score_history_state

    ws = InMemoryWorkspace()
    ScoreHistory(ws).record(_run_score("Solo"))
    assert collect_state(ws)["score_history"] == _score_history_state(ws)


# --- archived jobs: excluded from activity/reports/backlog by default --------


def _archived_workspace():
    ws = InMemoryWorkspace()
    _journal(ws, AgentEvent("engineer", "implement", "hidden"), run="assess-a")
    _journal(ws, AgentEvent("qa", "test", "visible"), run="assess-b")
    ws.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"repo": "a/b", "mode": "assess", "id": "assess-a",
                    "archived": True, "archived_at": 1.0}),
    )
    ws.write_text(
        "audit/assess-b/meta.json",
        json.dumps({"repo": "a/b", "mode": "assess", "id": "assess-b"}),
    )
    ws.write_text("audit/assess-a/assessment.md", "# hidden report")
    ws.write_text("audit/assess-b/assessment.md", "# visible report")
    store = BacklogStore(ws)
    backlog = store.load()
    epic = backlog.add_epic("Remediation")
    hidden = backlog.add_story("From archived job", "", epic_id=epic.id)
    hidden.source_job = "assess-a"
    visible = backlog.add_story("From live job", "", epic_id=epic.id)
    visible.source_job = "assess-b"
    backlog.add_story("No source job")
    store.save(backlog)
    return ws


def test_collect_state_excludes_archived_job_by_default():
    ws = _archived_workspace()
    state = collect_state(ws)
    assert state["archived_jobs"] == ["assess-a"]
    assert state["include_archived"] is False
    assert [r["run"] for r in state["activity"]] == ["assess-b"]
    assert [r["id"] for r in state["runs"]] == ["assess-b"]
    assert state["reports"] == ["audit/assess-b/assessment.md"]
    (epic_state,) = state["backlog"]["epics"]
    assert [s["title"] for s in epic_state["stories"]] == ["From live job"]
    assert [s["title"] for s in state["backlog"]["orphan_stories"]] == ["No source job"]


def test_collect_state_include_archived_reveals_everything():
    ws = _archived_workspace()
    state = collect_state(ws, include_archived=True)
    assert state["include_archived"] is True
    assert {r["run"] for r in state["activity"]} == {"assess-a", "assess-b"}
    assert {r["id"] for r in state["runs"]} == {"assess-a", "assess-b"}
    assert set(state["reports"]) == {
        "audit/assess-a/assessment.md", "audit/assess-b/assessment.md",
    }
    (epic_state,) = state["backlog"]["epics"]
    assert {s["title"] for s in epic_state["stories"]} == {
        "From archived job", "From live job",
    }
    # archived_jobs always lists every archived id, regardless of the view
    assert state["archived_jobs"] == ["assess-a"]


def test_collect_state_archive_round_trip_reappears_unmodified():
    # Mirrors the dispatch service's own archive_job/unarchive_job mutation
    # (flip the meta.json marker in place) rather than two static fixtures,
    # to prove the SAME on-disk transition round-trips through the state
    # the dashboard's three surfaces (activity, reports, backlog) render.
    ws = InMemoryWorkspace()
    _journal(ws, AgentEvent("engineer", "implement", "hello"), run="assess-r")
    ws.write_text("audit/assess-r/assessment.md", "# report")
    store = BacklogStore(ws)
    backlog = store.load()
    epic = backlog.add_epic("Remediation")
    story = backlog.add_story("From assess-r", "", epic_id=epic.id)
    story.source_job = "assess-r"
    store.save(backlog)
    live_meta = json.dumps({"repo": "a/b", "mode": "assess", "id": "assess-r"})
    archived_meta = json.dumps({
        "repo": "a/b", "mode": "assess", "id": "assess-r",
        "archived": True, "archived_at": 5.0,
    })
    ws.write_text("audit/assess-r/meta.json", live_meta)

    state = collect_state(ws)
    assert state["archived_jobs"] == []
    assert [r["run"] for r in state["activity"]] == ["assess-r"]

    ws.write_text("audit/assess-r/meta.json", archived_meta)
    state = collect_state(ws)
    assert state["archived_jobs"] == ["assess-r"]
    assert state["activity"] == []
    assert state["reports"] == []
    assert state["backlog"]["epics"][0]["stories"] == []

    ws.write_text("audit/assess-r/meta.json", live_meta)
    state = collect_state(ws)
    assert state["archived_jobs"] == []
    assert [r["run"] for r in state["activity"]] == ["assess-r"]
    assert state["reports"] == ["audit/assess-r/assessment.md"]
    assert [s["title"] for s in state["backlog"]["epics"][0]["stories"]] == [
        "From assess-r"
    ]


def test_archived_job_ids_tolerates_corrupt_and_non_matching_meta():
    from dev_team.dashboard import _archived_job_ids

    ws = InMemoryWorkspace()
    ws.write_text("audit/corrupt/meta.json", "{not json")
    ws.write_text("audit/wrong-name/assessment.json", "{}")  # not meta.json
    ws.write_text("audit/not-archived/meta.json", json.dumps({"archived": False}))
    ws.write_text("audit/archived/meta.json", json.dumps({"archived": True}))
    assert _archived_job_ids(ws) == frozenset({"archived"})


def test_report_job_id_extracts_the_owning_job_or_none():
    from dev_team.dashboard import _report_job_id

    assert _report_job_id("audit/assess-x/report.md") == "assess-x"
    assert _report_job_id("audit/assessment.md") is None
    assert _report_job_id("sub/audit/deep.md") is None


# --- calibration ---------------------------------------------------------------------


def test_calibration_state_empty_workspace():
    from dev_team.dashboard import _calibration_state

    assert _calibration_state(InMemoryWorkspace()) == {
        "phases": {},
        "overall": {
            "confirmed": 0, "refuted": 0, "needs_context": 0,
            "total": 0, "confirm_rate": None,
        },
        "jobs_counted": 0,
        "blind_spot_total": 0,
        "broken_citation_total": 0,
        "report_quality_jobs_counted": 0,
    }


def _verification_line(finding_id, verdict):
    return json.dumps({"finding_id": finding_id, "verdict": verdict})


def test_calibration_state_excludes_archived_job_by_default():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"id": "assess-a", "archived": True}),
    )
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        _verification_line("risk.secrets[0]", "confirmed") + "\n",
    )
    ws.write_text(
        "audit/assess-b/verifications.jsonl",
        _verification_line("risk.secrets[1]", "refuted") + "\n",
    )

    state = _calibration_state(ws)
    assert state["jobs_counted"] == 1
    assert state["phases"] == {
        "risk": {
            "confirmed": 0, "refuted": 1, "needs_context": 0,
            "total": 1, "confirm_rate": 0.0,
        }
    }
    assert state["overall"]["total"] == 1


def test_calibration_state_include_archived_reveals_everything():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"id": "assess-a", "archived": True}),
    )
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        _verification_line("risk.secrets[0]", "confirmed") + "\n",
    )
    ws.write_text(
        "audit/assess-b/verifications.jsonl",
        _verification_line("risk.secrets[1]", "refuted") + "\n",
    )

    state = _calibration_state(ws, include_archived=True)
    assert state["jobs_counted"] == 2
    assert state["phases"]["risk"] == {
        "confirmed": 1, "refuted": 1, "needs_context": 0,
        "total": 2, "confirm_rate": 0.5,
    }
    assert state["overall"]["total"] == 2


def test_calibration_state_tolerates_corrupt_line_and_counts_the_rest():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        "{not json\n" + _verification_line("qa.tests[0]", "confirmed") + "\n",
    )
    state = _calibration_state(ws)
    assert state["jobs_counted"] == 1
    assert state["overall"] == {
        "confirmed": 1, "refuted": 0, "needs_context": 0,
        "total": 1, "confirm_rate": 1.0,
    }


def test_calibration_state_skips_blank_lines_and_uncontributing_files():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        "\n   \n" + _verification_line("qa.tests[0]", "confirmed") + "\n",
    )
    ws.write_text("audit/assess-b/verifications.jsonl", "{not json\n\n")

    state = _calibration_state(ws)
    assert state["jobs_counted"] == 1
    assert state["overall"]["total"] == 1


def test_calibration_state_excludes_out_of_contract_entries():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        "\n".join(
            [
                _verification_line("qa.tests[0]", "maybe"),  # bad verdict
                _verification_line(None, "confirmed"),  # non-string finding_id
                _verification_line("qa.tests[1]", "confirmed"),
            ]
        )
        + "\n",
    )
    state = _calibration_state(ws)
    assert state["overall"] == {
        "confirmed": 1, "refuted": 0, "needs_context": 0,
        "total": 1, "confirm_rate": 1.0,
    }


def test_collect_state_calibration_key_matches_calibration_state():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"id": "assess-a", "archived": True}),
    )
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        _verification_line("risk.secrets[0]", "confirmed") + "\n",
    )
    ws.write_text(
        "audit/assess-b/verifications.jsonl",
        _verification_line("risk.secrets[1]", "refuted") + "\n",
    )

    for include_archived in (False, True):
        state = collect_state(ws, include_archived=include_archived)
        assert state["calibration"] == _calibration_state(
            ws, include_archived=include_archived
        )


def test_calibration_state_sums_blind_spots_and_broken_citations():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/assessment.json",
        json.dumps(
            {
                "blind_spots": ["legacy/", "vendor/"],
                "broken_citations": {"security": ["Web.config"], "qa": []},
            }
        ),
    )
    ws.write_text(
        "audit/assess-b/assessment.json",
        json.dumps({"blind_spots": ["docs/"], "broken_citations": {}}),
    )
    state = _calibration_state(ws)
    assert state["blind_spot_total"] == 3
    assert state["broken_citation_total"] == 1
    assert state["report_quality_jobs_counted"] == 2


def test_calibration_state_report_quality_excludes_jobs_with_no_assessment_json():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text("audit/deliver-a/meta.json", json.dumps({"mode": "deliver"}))
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        _verification_line("risk.secrets[0]", "confirmed") + "\n",
    )
    state = _calibration_state(ws)
    assert state["blind_spot_total"] == 0
    assert state["broken_citation_total"] == 0
    assert state["report_quality_jobs_counted"] == 0
    assert state["jobs_counted"] == 1


def test_calibration_state_report_quality_tolerates_malformed_or_wrong_typed_json():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text("audit/bad-json/assessment.json", "{not json")
    ws.write_text("audit/not-a-dict/assessment.json", json.dumps(["nope"]))
    ws.write_text(
        "audit/bad-blind-spots/assessment.json",
        json.dumps({"blind_spots": "not a list", "broken_citations": {}}),
    )
    ws.write_text(
        "audit/bad-broken-citations-type/assessment.json",
        json.dumps({"blind_spots": [], "broken_citations": "not a dict"}),
    )
    ws.write_text(
        "audit/bad-broken-citations-value/assessment.json",
        json.dumps({"blind_spots": [], "broken_citations": {"qa": "not a list"}}),
    )
    state = _calibration_state(ws)
    assert state["blind_spot_total"] == 0
    assert state["broken_citation_total"] == 0
    assert state["report_quality_jobs_counted"] == 0


def test_calibration_state_report_quality_excludes_archived_job_and_reappears():
    from dev_team.dashboard import _calibration_state

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"id": "assess-a", "archived": True}),
    )
    ws.write_text(
        "audit/assess-a/assessment.json",
        json.dumps({"blind_spots": ["legacy/"], "broken_citations": {"qa": ["x.py"]}}),
    )
    ws.write_text(
        "audit/assess-b/assessment.json",
        json.dumps({"blind_spots": ["docs/"], "broken_citations": {}}),
    )

    state = _calibration_state(ws)
    assert state["blind_spot_total"] == 1
    assert state["broken_citation_total"] == 0
    assert state["report_quality_jobs_counted"] == 1

    state = _calibration_state(ws, include_archived=True)
    assert state["blind_spot_total"] == 2
    assert state["broken_citation_total"] == 1
    assert state["report_quality_jobs_counted"] == 2


def test_dispatcher_calibration_and_calibration_state_parity():
    """AC5: Dispatcher.calibration() and _calibration_state() must agree
    over the same on-disk workspace fixture — the two hand-duplicated
    implementations must not drift on the new report-quality fields."""

    from dev_team.dashboard import _calibration_state
    from dev_team.dispatch import Dispatcher

    ws = InMemoryWorkspace()
    ws.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"id": "assess-a", "archived": True}),
    )
    ws.write_text(
        "audit/assess-a/assessment.json",
        json.dumps({"blind_spots": ["legacy/"], "broken_citations": {"qa": ["x.py"]}}),
    )
    ws.write_text(
        "audit/assess-a/verifications.jsonl",
        _verification_line("risk.secrets[0]", "confirmed") + "\n",
    )
    ws.write_text(
        "audit/assess-b/assessment.json",
        json.dumps({"blind_spots": ["docs/"], "broken_citations": {}}),
    )
    ws.write_text(
        "audit/assess-b/verifications.jsonl",
        _verification_line("risk.secrets[1]", "refuted") + "\n",
    )

    disp = Dispatcher(token="x", dashboard_workspace=ws)
    _, dispatcher_payload = disp.calibration()
    dashboard_payload = _calibration_state(ws)

    for key in (
        "blind_spot_total", "broken_citation_total", "report_quality_jobs_counted",
    ):
        assert dispatcher_payload[key] == dashboard_payload[key]


# --- report meta (blind spots / broken citations) ---------------------------------


def test_report_meta_state_counts_blind_spots_and_broken_citations():
    from dev_team.dashboard import _report_meta_state

    ws = InMemoryWorkspace()
    ws.write_text("audit/assess-a/assessment.md", "# report")
    ws.write_text(
        "audit/assess-a/assessment.json",
        json.dumps(
            {
                "blind_spots": ["legacy/", "vendor/"],
                "broken_citations": {"security": ["Web.config"], "qa": []},
            }
        ),
    )
    meta = _report_meta_state(ws)
    assert meta["assess-a"]["blind_spot_count"] == 2
    assert meta["assess-a"]["broken_citation_count"] == 1
    assert meta["assess-a"]["blind_spots"] == ["legacy/", "vendor/"]
    assert meta["assess-a"]["broken_citations"] == {"security": ["Web.config"], "qa": []}


def test_report_meta_state_missing_assessment_json_omits_job():
    from dev_team.dashboard import _report_meta_state

    ws = InMemoryWorkspace()
    ws.write_text("audit/assess-a/assessment.md", "# report")
    assert _report_meta_state(ws) == {}


def test_report_meta_state_tolerates_malformed_or_wrong_typed_json():
    from dev_team.dashboard import _report_meta_state

    ws = InMemoryWorkspace()
    ws.write_text("audit/bad-json/assessment.md", "# report")
    ws.write_text("audit/bad-json/assessment.json", "{not json")

    ws.write_text("audit/not-a-dict/assessment.md", "# report")
    ws.write_text("audit/not-a-dict/assessment.json", json.dumps(["nope"]))

    ws.write_text("audit/bad-blind-spots/assessment.md", "# report")
    ws.write_text(
        "audit/bad-blind-spots/assessment.json",
        json.dumps({"blind_spots": "not a list", "broken_citations": {}}),
    )

    ws.write_text("audit/bad-broken-citations-type/assessment.md", "# report")
    ws.write_text(
        "audit/bad-broken-citations-type/assessment.json",
        json.dumps({"blind_spots": [], "broken_citations": "not a dict"}),
    )

    ws.write_text("audit/bad-broken-citations-value/assessment.md", "# report")
    ws.write_text(
        "audit/bad-broken-citations-value/assessment.json",
        json.dumps({"blind_spots": [], "broken_citations": {"qa": "not a list"}}),
    )

    assert _report_meta_state(ws) == {}


def test_report_meta_state_ignores_report_paths_with_no_job_id():
    from dev_team.dashboard import _report_meta_state

    ws = InMemoryWorkspace()
    ws.write_text("audit/assessment.md", "# bare, non-dispatch report")
    # Even though a same-named assessment.json exists elsewhere in the
    # workspace, a report path with no owning job id must never pick it up.
    ws.write_text(
        "audit/assess-elsewhere/assessment.json",
        json.dumps({"blind_spots": ["x/"], "broken_citations": {}}),
    )
    meta = _report_meta_state(ws)
    assert meta == {}


def test_collect_state_report_meta_key_matches_report_meta_state_and_is_additive():
    from dev_team.dashboard import _report_meta_state

    ws = InMemoryWorkspace()
    ws.write_text("audit/assess-a/assessment.md", "# report")
    ws.write_text(
        "audit/assess-a/assessment.json",
        json.dumps({"blind_spots": ["legacy/"], "broken_citations": {}}),
    )
    state = collect_state(ws)
    assert state["report_meta"] == _report_meta_state(ws)
    assert state["reports"] == ["audit/assess-a/assessment.md"]


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
    assert set(state["calibration"]) == {
        "phases", "overall", "jobs_counted",
        "blind_spot_total", "broken_citation_total",
        "report_quality_jobs_counted",
    }


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


def test_login_cookie_is_not_secure_by_default(token_server):
    # Default bind is plain-HTTP localhost, where a Secure cookie would never
    # be stored — so back-compat means no Secure attribute unless opted in.
    status, headers, _ = _request(
        token_server, "POST", "/login", body=f"token={TOKEN}", headers=FORM
    )
    assert status == 303
    assert "Secure" not in headers["Set-Cookie"]


def test_login_cookie_is_secure_when_tls_enabled():
    # Opt-in Secure path: when the dashboard is fronted by TLS, the session
    # cookie is marked Secure (never sent over a plain connection) while
    # keeping HttpOnly and SameSite=Strict. Logout matches the attributes so
    # the browser actually overwrites it.
    ws = InMemoryWorkspace()
    _journal(ws, AgentEvent("engineer", "implement", "building"))
    srv = DashboardServer(ws, port=0, token=TOKEN, secure=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, _ = _request(
            srv, "POST", "/login", body=f"token={TOKEN}", headers=FORM
        )
        assert status == 303
        cookie = headers["Set-Cookie"]
        assert f"devteam_dash={TOKEN}" in cookie
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        status, headers, _ = _request(
            srv, "POST", "/logout", headers={"Cookie": f"devteam_dash={TOKEN}"}
        )
        assert status == 303
        logout_cookie = headers["Set-Cookie"]
        assert "Secure" in logout_cookie
        assert "Max-Age=0" in logout_cookie
    finally:
        srv.shutdown()
        thread.join(timeout=5)


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


# --- the job lifecycle proxy (/api/jobs/{id}/archive|unarchive → dispatch) ----


def test_jobs_proxy_forwards_archive_and_unarchive(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch, status=200, body=b'{"id": "assess-a", "archived": true}'
    )
    status, headers, body = _request(
        proxy_server, "POST", "/api/jobs/assess-a/archive", headers=AUTH
    )
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"id": "assess-a", "archived": True}
    status, _, _ = _request(
        proxy_server, "POST", "/api/jobs/assess-a/unarchive", headers=AUTH
    )
    assert status == 200
    archive_req, unarchive_req = seen
    assert archive_req.full_url == f"{DISPATCH_URL}/jobs/assess-a/archive"
    assert archive_req.get_method() == "POST"
    assert archive_req.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"
    assert unarchive_req.full_url == f"{DISPATCH_URL}/jobs/assess-a/unarchive"


def test_jobs_proxy_relays_a_dispatch_rejection(proxy_server, monkeypatch):
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/jobs/assess-a/archive", 409, "Conflict", None,
        io.BytesIO(b'{"error": "job is running"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, _, body = _request(
        proxy_server, "POST", "/api/jobs/assess-a/archive", headers=AUTH
    )
    assert status == 409
    assert json.loads(body) == {"error": "job is running"}


def test_jobs_proxy_rejects_actions_outside_archive_unarchive(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    # neither an unknown action nor a bare/deeper path is forwarded — the
    # proxy is scoped to exactly /api/jobs/{id}/archive|unarchive, never a
    # general passthrough to the dispatch job surface (which would let a
    # browser submit jobs with the dispatch token)
    for path in (
        "/api/jobs/assess-a/submit",
        "/api/jobs/assess-a",
        "/api/jobs/assess-a/archive/extra",
    ):
        status, _, body = _request(proxy_server, "POST", path, headers=AUTH)
        assert status == 404
        assert json.loads(body) == {"error": "not found"}
    assert seen == []


def test_jobs_proxy_unconfigured_is_501(token_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        token_server, "POST", "/api/jobs/assess-a/archive", headers=AUTH
    )
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "job actions not configured"}
    assert seen == []


def test_jobs_proxy_requires_dashboard_auth_first(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        proxy_server, "POST", "/api/jobs/assess-a/archive"
    )
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "unauthorized"}
    assert seen == []


# --- the job purge proxy (/api/jobs/{id}/purge → dispatch) -------------------


def test_jobs_proxy_forwards_purge(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch,
        status=200,
        body=b'{"id": "assess-a", "purged": true, '
        b'"removed": {"workspace": true, "audit": true, "backlog_stories": 0}}',
    )
    status, headers, body = _request(
        proxy_server, "POST", "/api/jobs/assess-a/purge", headers=AUTH
    )
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["purged"] is True
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/jobs/assess-a/purge"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"


def test_jobs_proxy_relays_a_purge_rejection(proxy_server, monkeypatch):
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/jobs/assess-a/purge", 409, "Conflict", None,
        io.BytesIO(b'{"error": "job is not archived"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, _, body = _request(
        proxy_server, "POST", "/api/jobs/assess-a/purge", headers=AUTH
    )
    assert status == 409
    assert json.loads(body) == {"error": "job is not archived"}


def test_jobs_proxy_purge_unconfigured_is_501(token_server, monkeypatch):
    # SECURITY: matches archive's own unconfigured 501 (no dispatch_url /
    # dispatch_token wired) — never silently forwards without one.
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        token_server, "POST", "/api/jobs/assess-a/purge", headers=AUTH
    )
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "job actions not configured"}
    assert seen == []


def test_jobs_proxy_purge_requires_dashboard_auth_first(proxy_server, monkeypatch):
    # SECURITY: matches archive's own auth-first behaviour — an
    # unauthenticated dashboard call never reaches the dispatch service.
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        proxy_server, "POST", "/api/jobs/assess-a/purge"
    )
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "unauthorized"}
    assert seen == []


def test_dashboard_html_purge_button_and_confirm_flow():
    # The "delete permanently" action only renders for an already-archived
    # job (purgeButton returns "" otherwise), forwards through the same
    # /api/jobs/{id}/... proxy as archive/unarchive, and is armed by a
    # two-step confirm identical to the story-delete confirm.
    assert "if (!archived) return" in DASHBOARD_HTML
    assert 'data-purgejob="${esc(id)}"' in DASHBOARD_HTML
    assert '"/api/jobs/" + encodeURIComponent(id) + "/purge"' in DASHBOARD_HTML
    assert '"confirm delete permanently?"' in DASHBOARD_HTML
    assert 'e.target.closest("[data-purgejob]")' in DASHBOARD_HTML


# --- the spend rollup proxy (GET /api/costs → dispatch GET /costs) -----------


def test_costs_proxy_forwards_and_relays_verbatim(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch,
        status=200,
        body=b'{"total_usd": 12.5, "by_mode": {"assess": 12.5}, "jobs_counted": 3}',
    )
    status, headers, body = _request(proxy_server, "GET", "/api/costs", headers=AUTH)
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {
        "total_usd": 12.5, "by_mode": {"assess": 12.5}, "jobs_counted": 3,
    }
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/costs"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"
    assert request.data is None


def test_costs_proxy_unconfigured_is_501(token_server, monkeypatch):
    # token_server has no dispatch_url/dispatch_token wired
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(token_server, "GET", "/api/costs", headers=AUTH)
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "spend rollup not configured"}
    assert seen == []


def test_costs_proxy_url_without_token_stays_unconfigured(monkeypatch):
    # A dispatch URL alone (no dispatch token) must not forward: still 501,
    # matching the board-editing and job-lifecycle proxies' own behaviour.
    seen = _capture_urlopen(monkeypatch)
    srv = DashboardServer(
        InMemoryWorkspace(), port=0, token=TOKEN, dispatch_url=DISPATCH_URL
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(srv, "GET", "/api/costs", headers=AUTH)
        assert status == 501
        assert json.loads(body) == {"error": "spend rollup not configured"}
        assert seen == []
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def test_costs_proxy_forwards_archived_flag_unchanged(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch, body=b'{"total_usd": 0, "by_mode": {}, "jobs_counted": 0}'
    )
    status, _, _ = _request(
        proxy_server, "GET", "/api/costs?archived=1", headers=AUTH
    )
    assert status == 200
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/costs?archived=1"


def test_costs_proxy_only_forwards_exact_archived_1(proxy_server, monkeypatch):
    # Any other/absent archived value defaults to excluding archived jobs,
    # matching GET /jobs's own exact-match contract.
    seen = _capture_urlopen(
        monkeypatch, body=b'{"total_usd": 0, "by_mode": {}, "jobs_counted": 0}'
    )
    for query in ("?archived=0", "?archived=true", ""):
        status, _, _ = _request(proxy_server, "GET", "/api/costs" + query, headers=AUTH)
        assert status == 200
    assert [r.full_url for r in seen] == [f"{DISPATCH_URL}/costs"] * 3


def test_costs_proxy_unreachable_dispatch_is_502(proxy_server, monkeypatch):
    _capture_urlopen(monkeypatch, error=urllib.error.URLError("refused"))
    status, _, body = _request(proxy_server, "GET", "/api/costs", headers=AUTH)
    assert status == 502
    assert json.loads(body) == {"error": "dispatch service unreachable"}
    assert "refused" not in body  # no internals leak


def test_costs_proxy_relays_a_dispatch_rejection_verbatim(proxy_server, monkeypatch):
    # e.g. a stale dispatch token: the dispatch service's own 401, never
    # swallowed or translated by the dashboard.
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/costs", 401, "Unauthorized", None,
        io.BytesIO(b'{"error": "unauthorized"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, _, body = _request(proxy_server, "GET", "/api/costs", headers=AUTH)
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}


def test_costs_proxy_requires_dashboard_auth_first(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(proxy_server, "GET", "/api/costs")
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "unauthorized"}
    assert seen == []  # nothing was ever forwarded


def test_costs_proxy_never_echoes_the_dispatch_token(proxy_server, monkeypatch):
    # SECURITY: the dispatch bearer token must never reach the browser, in
    # the response body or in any header — checked on the handler's
    # outgoing response, not just the (already-verbatim) proxied body.
    _capture_urlopen(
        monkeypatch,
        body=b'{"total_usd": 1.0, "by_mode": {"deliver": 1.0}, "jobs_counted": 1}',
    )
    status, headers, body = _request(proxy_server, "GET", "/api/costs", headers=AUTH)
    assert status == 200
    assert DISPATCH_TOKEN not in body
    assert DISPATCH_TOKEN not in str(headers)


def test_costs_route_scope_is_exact_match_only(proxy_server, monkeypatch):
    # SECURITY/scope: only exactly /api/costs is the spend route — a path
    # that merely starts with it falls through to the ordinary 404, never
    # forwarded to the dispatch service (no general dispatch passthrough).
    seen = _capture_urlopen(monkeypatch)
    for path in ("/api/costs/", "/api/costs/extra", "/api/costs2"):
        status, _, _ = _request(proxy_server, "GET", path, headers=AUTH)
        assert status == 404
    assert seen == []


def test_dashboard_html_spend_panel():
    # The Spend panel is fetched once on load plus on manual refresh only —
    # never inside the setInterval-driven refresh() poll (folding a proxied
    # dispatch hop into the 2.5s poll would multiply dispatch-service load
    # by open-tabs x poll-cadence for a number that only changes on job
    # completion).
    assert 'id="spend-refresh"' in DASHBOARD_HTML
    assert '<div class="panel" id="spend">' in DASHBOARD_HTML
    assert 'fetch("/api/costs")' in DASHBOARD_HTML
    assert "spend rollup not configured" in DASHBOARD_HTML
    assert '$("spend-refresh").addEventListener("click", loadSpend)' in DASHBOARD_HTML
    assert "data.total_usd" in DASHBOARD_HTML
    assert "data.by_mode" in DASHBOARD_HTML

    refresh_start = DASHBOARD_HTML.index("async function refresh()")
    refresh_end = DASHBOARD_HTML.index("refresh();", refresh_start)
    assert "/api/costs" not in DASHBOARD_HTML[refresh_start:refresh_end]
    # loadSpend's only two call sites are the manual-refresh click listener
    # and the one-time call alongside refresh() at page load — never inside
    # refresh() itself, and never wired to setInterval.
    assert 'addEventListener("click", loadSpend)' in DASHBOARD_HTML
    assert "setInterval(loadSpend" not in DASHBOARD_HTML
    assert "\nloadSpend();" in DASHBOARD_HTML


# --- the access log proxy (GET /api/access-log → dispatch GET /access-log) ---


def test_access_log_proxy_forwards_and_relays_verbatim(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch,
        status=200,
        body=b'{"entries": [{"ts": 1.0, "method": "GET", "path": "/jobs", "status": 200}]}',
    )
    status, headers, body = _request(
        proxy_server, "GET", "/api/access-log", headers=AUTH
    )
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {
        "entries": [{"ts": 1.0, "method": "GET", "path": "/jobs", "status": 200}],
    }
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/access-log"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"
    assert request.data is None


def test_access_log_proxy_unconfigured_is_501(token_server, monkeypatch):
    # token_server has no dispatch_url/dispatch_token wired
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        token_server, "GET", "/api/access-log", headers=AUTH
    )
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "access log not configured"}
    assert seen == []


def test_access_log_proxy_url_without_token_stays_unconfigured(monkeypatch):
    # A dispatch URL alone (no dispatch token) must not forward: still 501,
    # matching every other proxy's own behaviour.
    seen = _capture_urlopen(monkeypatch)
    srv = DashboardServer(
        InMemoryWorkspace(), port=0, token=TOKEN, dispatch_url=DISPATCH_URL
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(srv, "GET", "/api/access-log", headers=AUTH)
        assert status == 501
        assert json.loads(body) == {"error": "access log not configured"}
        assert seen == []
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def test_access_log_proxy_forwards_limit_unchanged(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch, body=b'{"entries": []}')
    status, _, _ = _request(
        proxy_server, "GET", "/api/access-log?limit=25", headers=AUTH
    )
    assert status == 200
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/access-log?limit=25"


def test_access_log_proxy_without_limit_forwards_no_query_string(
    proxy_server, monkeypatch
):
    seen = _capture_urlopen(monkeypatch, body=b'{"entries": []}')
    status, _, _ = _request(proxy_server, "GET", "/api/access-log", headers=AUTH)
    assert status == 200
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/access-log"


def test_access_log_proxy_unreachable_dispatch_is_502(proxy_server, monkeypatch):
    _capture_urlopen(monkeypatch, error=urllib.error.URLError("refused"))
    status, _, body = _request(proxy_server, "GET", "/api/access-log", headers=AUTH)
    assert status == 502
    assert json.loads(body) == {"error": "dispatch service unreachable"}
    assert "refused" not in body  # no internals leak


def test_access_log_proxy_relays_a_dispatch_rejection_verbatim(
    proxy_server, monkeypatch
):
    # e.g. a stale dispatch token: the dispatch service's own 401, never
    # swallowed or translated by the dashboard.
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/access-log", 401, "Unauthorized", None,
        io.BytesIO(b'{"error": "unauthorized"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, _, body = _request(proxy_server, "GET", "/api/access-log", headers=AUTH)
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}


def test_access_log_proxy_requires_dashboard_auth_first(proxy_server, monkeypatch):
    # SECURITY (AC8): unauthenticated GET /api/access-log answers 401, and
    # nothing is ever forwarded to the dispatch service.
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(proxy_server, "GET", "/api/access-log")
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "unauthorized"}
    assert seen == []  # nothing was ever forwarded


def test_access_log_proxy_never_echoes_the_dispatch_token(proxy_server, monkeypatch):
    # SECURITY: the dispatch bearer token must never reach the browser, in
    # the response body or in any header.
    _capture_urlopen(
        monkeypatch,
        body=b'{"entries": [{"ts": 1.0, "method": "GET", "path": "/x", "status": 200}]}',
    )
    status, headers, body = _request(
        proxy_server, "GET", "/api/access-log", headers=AUTH
    )
    assert status == 200
    assert DISPATCH_TOKEN not in body
    assert DISPATCH_TOKEN not in str(headers)


def test_access_log_route_scope_is_exact_match_only(proxy_server, monkeypatch):
    # SECURITY/scope: only exactly /api/access-log is the route — a path
    # that merely starts with it falls through to the ordinary 404, never
    # forwarded to the dispatch service (no general dispatch passthrough).
    seen = _capture_urlopen(monkeypatch)
    for path in ("/api/access-log/", "/api/access-log/extra", "/api/access-log2"):
        status, _, _ = _request(proxy_server, "GET", path, headers=AUTH)
        assert status == 404
    assert seen == []


def test_dashboard_html_access_log_panel():
    # AC9 + AC10: fetched once on load plus manual refresh only — never
    # inside the setInterval-driven refresh() poll (same load-multiplication
    # reason as Spend) — and every rendered field flows through esc() before
    # innerHTML: a logged `path` is arbitrary caller-supplied input (an
    # external caller can hit "/whatever<script>" and have it logged
    # verbatim, by design), so it must always render as inert text.
    assert 'id="access-log-refresh"' in DASHBOARD_HTML
    assert '<div class="panel" id="access-log">' in DASHBOARD_HTML
    assert 'fetch("/api/access-log")' in DASHBOARD_HTML
    assert "access log not configured" in DASHBOARD_HTML
    assert (
        '$("access-log-refresh").addEventListener("click", loadAccessLog)'
        in DASHBOARD_HTML
    )
    assert "${esc(e.method)}" in DASHBOARD_HTML
    assert "${esc(e.path)}" in DASHBOARD_HTML
    assert "${esc(e.status)}" in DASHBOARD_HTML

    refresh_start = DASHBOARD_HTML.index("async function refresh()")
    refresh_end = DASHBOARD_HTML.index("refresh();", refresh_start)
    assert "/api/access-log" not in DASHBOARD_HTML[refresh_start:refresh_end]
    assert "setInterval(loadAccessLog" not in DASHBOARD_HTML
    assert "\nloadAccessLog();" in DASHBOARD_HTML


# --- the foreman plan proxy (GET /api/foreman/plan → dispatch GET /foreman/plan) --


def test_foreman_plan_proxy_forwards_and_relays_verbatim(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch,
        status=200,
        body=b'{"ready_total": 2, "max_stories": 3, "plan": ['
        b'{"story_id": "S1", "title": "Remove hardcoded secret", "estimate": 1,'
        b' "repo": "acme/rota", "eligible": true, "reason": null}]}',
    )
    status, headers, body = _request(
        proxy_server, "GET", "/api/foreman/plan", headers=AUTH
    )
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {
        "ready_total": 2,
        "max_stories": 3,
        "plan": [
            {
                "story_id": "S1",
                "title": "Remove hardcoded secret",
                "estimate": 1,
                "repo": "acme/rota",
                "eligible": True,
                "reason": None,
            }
        ],
    }
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/foreman/plan"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"
    assert request.data is None


def test_foreman_plan_proxy_unconfigured_is_501(token_server, monkeypatch):
    # token_server has dashboard auth but no dispatch_url/dispatch_token wired
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        token_server, "GET", "/api/foreman/plan", headers=AUTH
    )
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "foreman plan not configured"}
    assert seen == []


def test_foreman_plan_proxy_requires_dashboard_auth_first(proxy_server, monkeypatch):
    # the reverse of the case above: dispatch IS configured (proxy_server),
    # but the caller never authenticated to the dashboard — 401 first, and
    # nothing is ever forwarded to the dispatch service.
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(proxy_server, "GET", "/api/foreman/plan")
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "unauthorized"}
    assert seen == []


def test_foreman_plan_proxy_url_without_token_stays_unconfigured(monkeypatch):
    # A dispatch URL alone (no dispatch token) must not forward: still 501,
    # matching every other proxy's own behaviour.
    seen = _capture_urlopen(monkeypatch)
    srv = DashboardServer(
        InMemoryWorkspace(), port=0, token=TOKEN, dispatch_url=DISPATCH_URL
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _request(srv, "GET", "/api/foreman/plan", headers=AUTH)
        assert status == 501
        assert json.loads(body) == {"error": "foreman plan not configured"}
        assert seen == []
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def test_foreman_plan_proxy_forwards_max_stories_unchanged(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch, body=b'{"ready_total": 0, "max_stories": 7, "plan": []}'
    )
    status, _, _ = _request(
        proxy_server, "GET", "/api/foreman/plan?max_stories=7", headers=AUTH
    )
    assert status == 200
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/foreman/plan?max_stories=7"


def test_foreman_plan_proxy_without_max_stories_forwards_no_query_string(
    proxy_server, monkeypatch
):
    seen = _capture_urlopen(
        monkeypatch, body=b'{"ready_total": 0, "max_stories": 3, "plan": []}'
    )
    status, _, _ = _request(proxy_server, "GET", "/api/foreman/plan", headers=AUTH)
    assert status == 200
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/foreman/plan"


def test_foreman_plan_proxy_forwards_an_out_of_range_max_stories_as_is(
    proxy_server, monkeypatch
):
    # No dashboard-side validation duplicated: a non-numeric or out-of-range
    # value is passed through unchanged, letting the dispatch service's own
    # [1, 10] clamp handle it.
    seen = _capture_urlopen(
        monkeypatch, body=b'{"ready_total": 0, "max_stories": 3, "plan": []}'
    )
    status, _, _ = _request(
        proxy_server, "GET", "/api/foreman/plan?max_stories=not-a-number", headers=AUTH
    )
    assert status == 200
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/foreman/plan?max_stories=not-a-number"


def test_foreman_plan_proxy_unreachable_dispatch_is_502(proxy_server, monkeypatch):
    _capture_urlopen(monkeypatch, error=urllib.error.URLError("refused"))
    status, _, body = _request(proxy_server, "GET", "/api/foreman/plan", headers=AUTH)
    assert status == 502
    assert json.loads(body) == {"error": "dispatch service unreachable"}
    assert "refused" not in body  # no internals leak


def test_foreman_plan_proxy_relays_a_dispatch_rejection_verbatim(
    proxy_server, monkeypatch
):
    # e.g. the dashboard workspace isn't configured dispatch-side: the
    # dispatch service's own 409, never swallowed or translated.
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/foreman/plan", 409, "Conflict", None,
        io.BytesIO(b'{"error": "the foreman needs a dashboard workspace"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, _, body = _request(proxy_server, "GET", "/api/foreman/plan", headers=AUTH)
    assert status == 409
    assert json.loads(body) == {"error": "the foreman needs a dashboard workspace"}


def test_foreman_plan_proxy_never_echoes_the_dispatch_token(proxy_server, monkeypatch):
    # SECURITY: the dispatch bearer token must never reach the browser, in
    # the response body or in any header.
    _capture_urlopen(
        monkeypatch,
        body=b'{"ready_total": 1, "max_stories": 3, "plan": []}',
    )
    status, headers, body = _request(
        proxy_server, "GET", "/api/foreman/plan", headers=AUTH
    )
    assert status == 200
    assert DISPATCH_TOKEN not in body
    assert DISPATCH_TOKEN not in str(headers)


def test_foreman_plan_route_scope_is_exact_match_only(proxy_server, monkeypatch):
    # SECURITY/scope: only exactly /api/foreman/plan is this route — a path
    # that merely starts with it, and in particular /api/foreman/run, falls
    # through to the ordinary 404, never forwarded to the dispatch service
    # (no /foreman/run wiring, no general dispatch passthrough).
    seen = _capture_urlopen(monkeypatch)
    for path in (
        "/api/foreman/plan/",
        "/api/foreman/plan/extra",
        "/api/foreman/plan2",
        "/api/foreman/run",
        "/api/foreman",
        "/api/foreman/",
    ):
        status, _, _ = _request(proxy_server, "GET", path, headers=AUTH)
        assert status == 404
    assert seen == []


def test_dashboard_html_foreman_plan_panel():
    # AC5/AC6/AC9: fetched once on load plus manual refresh only — never
    # inside the setInterval-driven refresh() poll (same load-multiplication
    # reason as Spend/Access log) — and every rendered field flows through
    # esc() before innerHTML: story_id/title/repo/reason can originate from
    # an LLM assessment finding (same provenance as the Story-detail panel),
    # so they must always render as inert text.
    assert 'id="foreman-plan-refresh"' in DASHBOARD_HTML
    assert '<div class="panel" id="foreman-plan">' in DASHBOARD_HTML
    assert 'fetch("/api/foreman/plan")' in DASHBOARD_HTML
    assert "foreman plan not configured" in DASHBOARD_HTML
    assert "failed to load foreman plan" in DASHBOARD_HTML
    assert "nothing ready to deliver" in DASHBOARD_HTML
    assert (
        '$("foreman-plan-refresh").addEventListener("click", loadForemanPlan)'
        in DASHBOARD_HTML
    )
    assert "${esc(entry.story_id)}" in DASHBOARD_HTML
    assert "${esc(entry.title)}" in DASHBOARD_HTML
    assert "${esc(entry.repo)}" in DASHBOARD_HTML
    assert "${esc(entry.reason)}" in DASHBOARD_HTML
    assert "${esc(data.ready_total)}" in DASHBOARD_HTML
    assert "${esc(data.max_stories)}" in DASHBOARD_HTML

    refresh_start = DASHBOARD_HTML.index("async function refresh()")
    refresh_end = DASHBOARD_HTML.index("refresh();", refresh_start)
    assert "/api/foreman/plan" not in DASHBOARD_HTML[refresh_start:refresh_end]
    assert "setInterval(loadForemanPlan" not in DASHBOARD_HTML
    assert "\nloadForemanPlan();" in DASHBOARD_HTML


# --- the pending-question proxy (GET /api/jobs/{id}/question → dispatch) -----


def test_question_proxy_forwards_and_relays_verbatim(proxy_server, monkeypatch):
    seen = _capture_urlopen(
        monkeypatch,
        body=b'{"pending": true, "prompt": "merge now?", "context": "plan review",'
        b' "choices": [{"key": "yes", "label": "Yes", "accepts_text": false}],'
        b' "default": "yes"}',
    )
    status, headers, body = _request(
        proxy_server, "GET", "/api/jobs/deliver-1/question", headers=AUTH
    )
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {
        "pending": True,
        "prompt": "merge now?",
        "context": "plan review",
        "choices": [{"key": "yes", "label": "Yes", "accepts_text": False}],
        "default": "yes",
    }
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/jobs/deliver-1/question"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"
    assert request.data is None


def test_question_proxy_relays_no_pending_question(proxy_server, monkeypatch):
    _capture_urlopen(monkeypatch, body=b'{"pending": false}')
    status, _, body = _request(
        proxy_server, "GET", "/api/jobs/deliver-1/question", headers=AUTH
    )
    assert status == 200
    assert json.loads(body) == {"pending": False}


def test_question_proxy_relays_unknown_job_404_verbatim(proxy_server, monkeypatch):
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/jobs/nope/question", 404, "Not Found", None,
        io.BytesIO(b'{"error": "unknown job"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, _, body = _request(
        proxy_server, "GET", "/api/jobs/nope/question", headers=AUTH
    )
    assert status == 404
    assert json.loads(body) == {"error": "unknown job"}


def test_question_proxy_unconfigured_is_501(token_server, monkeypatch):
    # token_server has no dispatch_url/dispatch_token wired
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        token_server, "GET", "/api/jobs/deliver-1/question", headers=AUTH
    )
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "pending question not configured"}
    assert seen == []


def test_question_proxy_requires_dashboard_auth_first(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        proxy_server, "GET", "/api/jobs/deliver-1/question"
    )
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "unauthorized"}
    assert seen == []


def test_question_proxy_never_echoes_the_dispatch_token(proxy_server, monkeypatch):
    # SECURITY: matches the costs/backlog proxies' own non-leak assertion —
    # the dispatch bearer token must never reach the browser.
    _capture_urlopen(
        monkeypatch, body=b'{"pending": true, "prompt": "p", "context": "",'
        b' "choices": [], "default": "yes"}',
    )
    status, headers, body = _request(
        proxy_server, "GET", "/api/jobs/deliver-1/question", headers=AUTH
    )
    assert status == 200
    assert DISPATCH_TOKEN not in body
    assert DISPATCH_TOKEN not in str(headers)


def test_question_route_scope_is_exact_match_only(proxy_server, monkeypatch):
    # SECURITY/scope: a path that merely starts with the jobs prefix but
    # doesn't exactly match .../question falls through to the ordinary 404,
    # never forwarded — proving the suffix match is exact, not a loose
    # startswith/in check.
    seen = _capture_urlopen(monkeypatch)
    for path in (
        "/api/jobs/deliver-1",
        "/api/jobs/deliver-1/question/extra",
        "/api/jobs/deliver-1/answered",
    ):
        status, _, body = _request(proxy_server, "GET", path, headers=AUTH)
        assert status == 404
    assert seen == []


# --- the interactive answer proxy (POST /api/jobs/{id}/answer → dispatch) ----


def test_answer_proxy_forwards_body_and_relays_verbatim(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch, status=202, body=b"{}")
    payload = json.dumps({"choice": "yes", "text": ""})
    status, headers, body = _request(
        proxy_server, "POST", "/api/jobs/deliver-1/answer",
        headers={**AUTH, "Content-Type": "application/json"}, body=payload,
    )
    assert status == 202
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {}
    (request,) = seen
    assert request.full_url == f"{DISPATCH_URL}/jobs/deliver-1/answer"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {DISPATCH_TOKEN}"
    assert request.data == payload.encode()


def test_answer_proxy_tolerates_no_body(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch, status=400, body=b'{"error": "unknown choice"}')
    status, _, body = _request(
        proxy_server, "POST", "/api/jobs/deliver-1/answer", headers=AUTH
    )
    assert status == 400
    assert json.loads(body) == {"error": "unknown choice"}
    (request,) = seen
    assert request.data is None  # no body sent → none forwarded


def test_answer_proxy_tolerates_a_malformed_content_length(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch, status=202, body=b"{}")
    status, _, _ = _request(
        proxy_server, "POST", "/api/jobs/deliver-1/answer",
        headers={**AUTH, "Content-Length": "xyz"},
    )
    assert status == 202
    (request,) = seen
    assert request.data is None  # unreadable length → treated as no body


def test_answer_proxy_relays_a_dispatch_rejection(proxy_server, monkeypatch):
    rejection = urllib.error.HTTPError(
        f"{DISPATCH_URL}/jobs/deliver-1/answer", 409, "Conflict", None,
        io.BytesIO(b'{"error": "no pending question"}'),
    )
    _capture_urlopen(monkeypatch, error=rejection)
    status, _, body = _request(
        proxy_server, "POST", "/api/jobs/deliver-1/answer",
        headers=AUTH, body=json.dumps({"choice": "yes"}),
    )
    assert status == 409
    assert json.loads(body) == {"error": "no pending question"}


def test_answer_proxy_unconfigured_is_501(token_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        token_server, "POST", "/api/jobs/deliver-1/answer",
        headers=AUTH, body=json.dumps({"choice": "yes"}),
    )
    assert status == 501
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "job actions not configured"}
    assert seen == []


def test_answer_proxy_requires_dashboard_auth_first(proxy_server, monkeypatch):
    seen = _capture_urlopen(monkeypatch)
    status, headers, body = _request(
        proxy_server, "POST", "/api/jobs/deliver-1/answer"
    )
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "unauthorized"}
    assert seen == []


def test_answer_proxy_never_echoes_the_dispatch_token(proxy_server, monkeypatch):
    # SECURITY: matches the question/costs/backlog proxies' own non-leak
    # assertion — the dispatch bearer token must never reach the browser.
    _capture_urlopen(monkeypatch, status=202, body=b"{}")
    status, headers, body = _request(
        proxy_server, "POST", "/api/jobs/deliver-1/answer",
        headers=AUTH, body=json.dumps({"choice": "yes"}),
    )
    assert status == 202
    assert DISPATCH_TOKEN not in body
    assert DISPATCH_TOKEN not in str(headers)


def test_answer_route_is_kept_out_of_the_no_body_jobs_actions(proxy_server, monkeypatch):
    # SECURITY/scope: "answer" is not in _JOBS_PROXY_ACTIONS, so a path that
    # merely resembles it (wrong exact suffix) still falls through to the
    # ordinary 404, never forwarded — same exact-match discipline as the
    # question route above.
    seen = _capture_urlopen(monkeypatch)
    for path in (
        "/api/jobs/deliver-1/answered",
        "/api/jobs/deliver-1/answer/extra",
    ):
        status, _, body = _request(proxy_server, "POST", path, headers=AUTH)
        assert status == 404
        assert json.loads(body) == {"error": "not found"}
    assert seen == []


def test_dashboard_html_question_panel():
    # Static desk-check of the pending-question panel JS (CI has no
    # browser), mirroring test_dashboard_html_spend_panel's approach:
    # only running, non-archived jobs get a placeholder to poll, the panel
    # renders nothing on an absent/unconfigured question and the prompt
    # plus one button per choice when one is pending, and polling is scoped
    # (running jobs only) and visibility-gated, on its own interval kept
    # out of the 2.5s /api/state poll.
    assert 'function runningJobIds(s)' in DASHBOARD_HTML
    assert 'id="q-${esc(r.id)}"' in DASHBOARD_HTML
    assert 'if (!data || !data.pending) return ""' in DASHBOARD_HTML
    assert 'data-answer="${esc(id)}"' in DASHBOARD_HTML
    assert 'data-choice="${esc(c.key)}"' in DASHBOARD_HTML
    assert "c.accepts_text" in DASHBOARD_HTML
    assert 'fetch("/api/jobs/" + encodeURIComponent(id) + "/question")' in DASHBOARD_HTML
    assert 'fetch("/api/jobs/" + encodeURIComponent(id) + "/answer"' in DASHBOARD_HTML
    assert 'document.visibilityState !== "visible"' in DASHBOARD_HTML
    assert "setInterval(pollQuestions, 5000)" in DASHBOARD_HTML

    refresh_start = DASHBOARD_HTML.index("async function refresh()")
    refresh_end = DASHBOARD_HTML.index("refresh();", refresh_start)
    assert "/api/jobs/" not in DASHBOARD_HTML[refresh_start:refresh_end]


# --- /api/state?archived=1 ----------------------------------------------------


def test_state_route_archived_query_param(server):
    status, _, body = _request(server, "GET", "/api/state")
    assert status == 200
    assert json.loads(body)["include_archived"] is False
    status, _, body = _request(server, "GET", "/api/state?archived=1")
    assert status == 200
    assert json.loads(body)["include_archived"] is True
