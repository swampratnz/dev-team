"""Tests for the authenticated HTTP dispatch service."""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from helpers import engine_responses
from test_assessment import assess_responses

from dev_team import __version__
from dev_team import dispatch as dispatch_mod
from dev_team.accesslog import read_access_log
from dev_team.approval import ApprovalRequest, PolicyApprovalGate
from dev_team.backlog import BacklogStore
from dev_team.budget import Budget
from dev_team.policy import EXIT_DENIED
from dev_team.sdk import AgentResult
from dev_team.dispatch import (
    Dispatcher,
    DispatchServer,
    JobRecord,
    JobSpec,
    QueueFull,
    SubmitRejected,
    ValidationError,
    _default_materialise,
)
from dev_team.eventlog import read_events
from dev_team.execution import InMemoryWorkspace, LocalWorkspace
from dev_team.interaction import Choice, Question, Reply
from dev_team.testing import ScriptedRunner

TOKEN = "s3cr3t-token"

# A minimal non-empty file set so a fake clone looks like a real checkout: the
# assess engine refuses an empty workspace before spending on any agent, and a
# real clone is never empty. Content is irrelevant to the scripted assess
# responses — only that files exist.
_CLONE_FILES = {"README.md": "# repo", "src/app.py": "x = 1"}


def _clone_ws():
    """A fresh in-memory workspace seeded like a real (non-empty) clone."""

    return InMemoryWorkspace(dict(_CLONE_FILES))


def _mem_materialise(spec, dest):
    """A fake clone: no disk, no network — just a fresh in-memory workspace."""

    return _clone_ws()


def _assess_runner():
    return ScriptedRunner(by_system_prompt=assess_responses())


def _deliver_runner():
    return ScriptedRunner(by_system_prompt=engine_responses())


# --- build_spec validation ----------------------------------------------------


def test_build_spec_assess_defaults_title_to_slug_and_blank_description():
    spec = Dispatcher(token="x").build_spec({"mode": "assess", "repo": "acme/mono"})
    assert spec.mode == "assess"
    assert spec.title == "acme/mono"
    assert spec.description == ""
    assert spec.budget_usd is None


def test_build_spec_assess_keeps_supplied_title_and_description():
    spec = Dispatcher(token="x").build_spec(
        {"mode": "assess", "repo": "acme/mono", "title": "Audit", "description": "dig"}
    )
    assert spec.title == "Audit"
    assert spec.description == "dig"


def test_build_spec_assess_non_string_description_becomes_blank():
    spec = Dispatcher(token="x").build_spec(
        {"mode": "assess", "repo": "acme/mono", "description": 123}
    )
    assert spec.description == ""


def test_build_spec_deliver_requires_title_and_description():
    disp = Dispatcher(token="x")
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "T", "description": "D"}
    )
    assert spec.mode == "deliver"
    with pytest.raises(ValidationError):
        disp.build_spec({"mode": "deliver", "repo": "acme/mono", "description": "D"})
    with pytest.raises(ValidationError):
        disp.build_spec({"mode": "deliver", "repo": "acme/mono", "title": "T"})


def test_build_spec_rejects_bad_mode_and_repo():
    disp = Dispatcher(token="x")
    with pytest.raises(ValidationError):
        disp.build_spec({"mode": "nope", "repo": "acme/mono"})
    with pytest.raises(ValidationError):
        disp.build_spec({"repo": "acme/mono"})  # missing mode
    with pytest.raises(ValidationError):
        disp.build_spec({"mode": "assess"})  # missing repo (not a string)
    with pytest.raises(ValidationError):
        disp.build_spec({"mode": "assess", "repo": "   "})  # empty repo
    with pytest.raises(ValidationError):
        disp.build_spec({"mode": "assess", "repo": "%%%"})  # unparseable


def test_build_spec_validates_budget():
    disp = Dispatcher(token="x")
    assert disp.build_spec({"mode": "assess", "repo": "a/b", "budget_usd": 5}).budget_usd == 5
    assert disp.build_spec(
        {"mode": "assess", "repo": "a/b", "budget_usd": None}
    ).budget_usd is None
    for bad in (True, "5", 0, -1):
        with pytest.raises(ValidationError):
            disp.build_spec({"mode": "assess", "repo": "a/b", "budget_usd": bad})


def test_build_spec_backlog_defaults_false_and_must_be_bool():
    disp = Dispatcher(token="x")
    assert disp.build_spec({"mode": "assess", "repo": "a/b"}).backlog is False
    assert disp.build_spec(
        {"mode": "assess", "repo": "a/b", "backlog": True}
    ).backlog is True
    assert disp.build_spec(
        {"mode": "assess", "repo": "a/b", "backlog": False}
    ).backlog is False
    for bad in (1, 0, "true", None, [], {}):
        with pytest.raises(ValidationError):
            disp.build_spec({"mode": "assess", "repo": "a/b", "backlog": bad})


# --- submit / registry --------------------------------------------------------


def test_submit_assigns_positions_and_recent_is_newest_first():
    disp = Dispatcher(token="x")  # worker never started, so jobs stay queued
    id1, pos1 = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
    id2, pos2 = disp.submit(disp.build_spec({"mode": "deliver", "repo": "a/two",
                                             "title": "T", "description": "D"}))
    assert (pos1, pos2) == (0, 1)
    assert [r.spec.id for r in disp.recent()] == [id2, id1]
    assert disp.get(id1).state == "queued"
    assert disp.get("unknown") is None


def test_recent_paginates_with_bounded_limit_and_offset():
    # U10(c): >25 jobs must be reachable via limit/offset, with the defaults
    # reproducing the historical newest-first cap of 25.
    disp = Dispatcher(token="x", queue_cap=100)  # worker not started: all queued
    ids = [
        disp.submit(disp.build_spec({"mode": "assess", "repo": f"a/r{i}"}))[0]
        for i in range(30)
    ]
    newest_first = list(reversed(ids))
    assert [r.spec.id for r in disp.recent()] == newest_first[:25]  # default cap kept
    assert [r.spec.id for r in disp.recent(limit=10)] == newest_first[:10]
    assert [r.spec.id for r in disp.recent(limit=10, offset=10)] == newest_first[10:20]
    assert disp.recent(offset=100) == []  # offset past the end is empty, not an error
    # bounds are clamped: limit floors to 1 and ceils to _LIST_LIMIT_MAX,
    # a negative offset floors to 0.
    assert len(disp.recent(limit=0)) == 1
    assert len(disp.recent(limit=10_000)) == 30  # clamp to 100, only 30 exist
    assert [r.spec.id for r in disp.recent(offset=-5, limit=3)] == newest_first[:3]


def test_submit_raises_queue_full_at_cap():
    disp = Dispatcher(token="x", queue_cap=2)
    disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
    disp.submit(disp.build_spec({"mode": "assess", "repo": "a/two"}))
    with pytest.raises(QueueFull):
        disp.submit(disp.build_spec({"mode": "assess", "repo": "a/three"}))


def test_wait_on_unknown_job_returns_false():
    assert Dispatcher(token="x").wait("nope", timeout=0.1) is False


def test_start_is_idempotent_and_stop_without_start_is_safe():
    disp = Dispatcher(token="x")
    disp.stop()  # never started — the thread-None branch
    disp.start()
    disp.start()  # second call is a no-op
    disp.stop()


# --- run_job / worker (offline, injected fakes) -------------------------------


def test_run_job_assess_path():
    disp = Dispatcher(token="x", runner=_assess_runner(), materialise=_mem_materialise)
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-x"
    outcome, cost = asyncio.run(disp.run_job(JobRecord(spec=spec)))
    assert outcome.success is True
    assert outcome.classification == "dependency-surgery"
    assert cost == 0.0


def test_run_job_deliver_path():
    disp = Dispatcher(token="x", runner=_deliver_runner(), materialise=_mem_materialise)
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d"}
    )
    spec.id = "deliver-x"
    outcome, cost = asyncio.run(disp.run_job(JobRecord(spec=spec)))
    assert outcome.success is True


def test_dispatch_deliver_gates_high_risk_commands(monkeypatch):
    # S1: unattended dispatch delivery must not run with the no-op
    # AutoApprover, which auto-grants the high-risk push/deploy/rm gate.
    # run_job must hand team.deliver a PolicyApprovalGate(block_risks=("high",))
    # so a dispatched deliver still commits the risk="medium" feature but a
    # high-risk push/deploy is denied. Spy on make_engine to capture both the
    # approval gate wired in and the guarded command runner it built.
    captured = {}
    real_make_engine = dispatch_mod.DevTeam.make_engine

    def spy_make_engine(self, **kwargs):
        engine = real_make_engine(self, **kwargs)
        captured["approval"] = kwargs.get("approval")
        captured["engine"] = engine
        return engine

    monkeypatch.setattr(dispatch_mod.DevTeam, "make_engine", spy_make_engine)

    disp = Dispatcher(token="x", runner=_deliver_runner(), materialise=_mem_materialise)
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d"}
    )
    spec.id = "deliver-gate"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))

    approval = captured["approval"]
    assert isinstance(approval, PolicyApprovalGate)
    # The engine's guarded command runner denies a high-risk push/deploy ...
    runner = captured["engine"].command_runner
    assert runner.run(["git", "push"]).exit_code == EXIT_DENIED
    assert runner.run(["deploy", "prod"]).exit_code == EXIT_DENIED
    # ... while the risk="medium" feature commit is still auto-approved, so
    # dispatch delivery keeps committing.
    assert approval.review(
        ApprovalRequest(action="commit feature", detail="", risk="medium")
    ).approved is True


def test_worker_runs_a_successful_job():
    disp = Dispatcher(token="x", runner=_assess_runner(), materialise=_mem_materialise)
    disp.start()
    try:
        job_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "acme/mono"}))
        assert disp.wait(job_id, 5) is True
        record = disp.get(job_id)
        assert record.state == "succeeded"
        assert record.started is not None and record.ended is not None
        assert record.cost_usd == 0.0
    finally:
        disp.stop()


def test_worker_marks_a_failing_job_failed():
    def boom(spec, dest):
        raise RuntimeError("clone exploded")

    disp = Dispatcher(token="x", materialise=boom)
    disp.start()
    try:
        job_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "acme/mono"}))
        assert disp.wait(job_id, 5) is True
        record = disp.get(job_id)
        assert record.state == "failed"
        assert "clone exploded" in record.error
        # The clone raised before any Budget was created, so the recorded cost
        # is a genuine 0.0 (no budget attached) — not the old hard-coded 0.
        assert record.budget is None
        assert record.cost_usd == 0.0
    finally:
        disp.stop()


def test_failed_job_reports_real_partial_spend():
    # E4/U6: a job that burned budget before raising must report that partial
    # spend, not a hard-coded 0.0. Drive _execute with a run_job that attaches
    # a budget, spends on it, then raises — exactly the mid-run-failure shape.
    disp = Dispatcher(token="x")
    spec = JobSpec(mode="assess", repo="a/b", title="t", description="",
                   budget_usd=None, id="assess-spend")
    record = JobRecord(spec=spec)
    disp._registry[spec.id] = record
    disp._events[spec.id] = threading.Event()

    async def spend_then_raise(rec):
        budget = Budget()
        rec.budget = budget
        budget.record("architect", AgentResult(text="", cost_usd=0.75, num_turns=1))
        raise RuntimeError("blew up after spending")

    disp.run_job = spend_then_raise  # instance override; _execute calls self.run_job
    asyncio.run(disp._execute(spec.id))

    assert record.state == "failed"
    assert "blew up after spending" in record.error
    assert record.budget.spent == 0.75
    assert record.cost_usd == record.budget.spent > 0
    # /result and /costs both serve the real partial spend, not 0
    assert disp.result(record) == (
        200,
        {"kind": "assess", "success": False,
         "error": record.error, "cost_usd": 0.75},
    )
    assert disp.costs() == (
        200, {"total_usd": 0.75, "by_mode": {"assess": 0.75}, "jobs_counted": 1})


def test_worker_times_out_a_hung_job_and_keeps_serving():
    # E5(a): a job whose _execute hangs at an await point must not wedge the
    # single-flight worker — it is aborted past the injected wall-clock ceiling,
    # marked failed with a timeout error, and the worker serves the next job.
    started = threading.Event()

    async def hang(record):
        started.set()
        await asyncio.sleep(3600)  # far past the 0.05s ceiling; never resolves

    disp = Dispatcher(
        token="x", runner=_assess_runner(), materialise=_mem_materialise,
        job_timeout=0.05,
    )
    disp.run_job = hang  # first job hangs; instance override _execute calls
    disp.start()
    try:
        hung_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/hang"}))
        assert started.wait(5)
        assert disp.wait(hung_id, 5) is True
        record = disp.get(hung_id)
        assert record.state == "failed"
        assert "time limit" in record.error
        assert record.cost_usd == 0.0  # no budget was ever created for it

        # The worker survived the timeout: restore the real run_job and prove
        # the next job still runs to completion.
        del disp.run_job
        next_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "acme/mono"}))
        assert disp.wait(next_id, 5) is True
        assert disp.get(next_id).state == "succeeded"
    finally:
        disp.stop()


def test_run_job_mirrors_events_and_report_into_the_dashboard_workspace():
    dash = InMemoryWorkspace()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=_mem_materialise,
        dashboard_workspace=dash,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-dash"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    # Events are journalled to the shared dashboard workspace under the same
    # run id, so the standing --dashboard process shows this job as its own run.
    mirrored = read_events(dash)
    assert mirrored, "dispatched job events should reach the dashboard workspace"
    assert all(e["run"] == "assess-dash" for e in mirrored)
    # The assess report is mirrored under a per-job audit/<id>/ path.
    assert dash.exists("audit/assess-dash/assessment.md")
    assert dash.read_text("audit/assess-dash/assessment.md")


def test_run_job_deliver_mirrors_events_but_writes_no_report():
    dash = InMemoryWorkspace()
    disp = Dispatcher(
        token="x",
        runner=_deliver_runner(),
        materialise=_mem_materialise,
        dashboard_workspace=dash,
    )
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d"}
    )
    spec.id = "deliver-dash"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    assert read_events(dash), "deliver events should still reach the dashboard"
    # A delivery outcome has no report_markdown, so nothing is written to audit/.
    assert not any(p.startswith("audit/") for p in dash.list_files())


def test_run_job_records_transcripts_into_the_dashboard_workspace():
    from dev_team.transcripts import list_transcripts

    dash = InMemoryWorkspace()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=_mem_materialise,
        dashboard_workspace=dash,
        record_transcripts=True,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-tx"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    # transcripts land in the shared dashboard workspace, keyed by the job id
    assert list_transcripts(dash, "assess-tx", "architect")


def test_run_job_records_transcripts_into_job_workspace_without_dashboard():
    from dev_team.transcripts import list_transcripts

    job_ws = _clone_ws()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=lambda spec, dest: job_ws,
        record_transcripts=True,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-solo"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    # with no dashboard workspace, transcripts fall back to the job's own
    assert list_transcripts(job_ws, "assess-solo", "architect")


def test_run_job_does_not_record_transcripts_by_default():
    from dev_team.transcripts import list_transcripts

    dash = InMemoryWorkspace()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=_mem_materialise,
        dashboard_workspace=dash,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-off"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    assert list_transcripts(dash, "assess-off", "architect") == []


def test_run_job_journals_a_trace_log_always_on_without_record_transcripts():
    from dev_team.tracelog import read_trace_log

    job_ws = _clone_ws()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=lambda spec, dest: job_ws,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-trace"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    spans = read_trace_log(job_ws)
    assert spans, "assess job left no trace journal"
    assert all(s["run"] == "assess-trace" for s in spans)


def test_mirror_report_is_a_noop_without_a_dashboard_workspace():
    # No dashboard configured → returns immediately, touches nothing.
    Dispatcher(token="x")._mirror_report("job-x", object())


def test_mirror_report_skips_an_outcome_with_no_report():
    # Defensive guard: an assess outcome that produced no report markdown must
    # not write an empty file into the dashboard's Reports panel.
    dash = InMemoryWorkspace()
    disp = Dispatcher(token="x", dashboard_workspace=dash)

    class _NoReport:
        report_markdown = ""

    disp._mirror_report("job-x", _NoReport())
    assert not any(p.startswith("audit/") for p in dash.list_files())


def test_run_job_mirrors_assessment_json_into_the_dashboard_workspace():
    dash = InMemoryWorkspace()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=_mem_materialise,
        dashboard_workspace=dash,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-json"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    # The structured result lands beside the markdown report — this file is
    # what POST /jobs/{id}/backlog reads later, even after a restart.
    data = json.loads(dash.read_text("audit/assess-json/assessment.json"))
    assert data["classification"] == "dependency-surgery"
    assert data["phases"]["recommendation"]["ok"] is True


def test_run_job_assess_with_backlog_updates_job_and_dashboard_backlogs():
    from dev_team.backlog import BacklogStore

    dash = InMemoryWorkspace()
    job_ws = _clone_ws()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=lambda spec, dest: job_ws,
        dashboard_workspace=dash,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono", "backlog": True})
    spec.id = "assess-bl"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    # update_backlog=True wrote the job workspace's own backlog ...
    job_backlog = BacklogStore(job_ws).load()
    assert job_backlog.stories
    # ... and the same stories were merged into the dashboard workspace so
    # its backlog panel shows them immediately.
    dash_backlog = BacklogStore(dash).load()
    assert {s.title for s in dash_backlog.stories} == {
        s.title for s in job_backlog.stories
    }
    # The dashboard merge knows the job's repo and id, so its stories file
    # under the repo's own epic and carry finding provenance ...
    assert dash_backlog.epics[0].title == "Remediation — acme/mono"
    assert all(s.source_job == "assess-bl" for s in dash_backlog.stories)
    # ... while the job workspace's own backlog (written by the engine,
    # which has no job context) keeps the historical single epic.
    assert job_backlog.epics[0].title == "Assessment remediation"


def test_run_job_assess_with_backlog_but_no_dashboard_workspace():
    from dev_team.backlog import BacklogStore

    job_ws = _clone_ws()
    disp = Dispatcher(
        token="x", runner=_assess_runner(), materialise=lambda spec, dest: job_ws
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono", "backlog": True})
    spec.id = "assess-bl-solo"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    assert BacklogStore(job_ws).load().stories  # merge skipped, job backlog kept


def _assessment_payload():
    """A minimal outcome_to_dict-shaped payload with one plan step."""

    return {
        "classification": "dependency-surgery",
        "phases": {
            "recommendation": {
                "role": "product-manager",
                "ok": True,
                "error": None,
                "data": {
                    "plan": [
                        {"step": "Pin build chain", "effort": "2 days", "detail": "CI"}
                    ]
                },
            }
        },
        "dead_code": {"findings": []},
        "dependency_scan": {"vulnerabilities": []},
    }


def test_make_backlog_survives_a_restart_by_reading_disk():
    from dev_team.backlog import BacklogStore

    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-old/assessment.json", json.dumps(_assessment_payload())
    )
    # A FRESH dispatcher: empty in-memory registry, as after a service
    # restart — the endpoint must work from the persisted JSON alone.
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.make_backlog("assess-old")
    assert (status, payload) == (
        200,
        {"job_id": "assess-old", "stories_added": 1, "stories_total": 1},
    )
    stored = BacklogStore(dash).load()
    assert [s.title for s in stored.stories] == ["Pin build chain"]
    # a second call dedupes by title instead of flooding
    status, payload = disp.make_backlog("assess-old")
    assert (status, payload) == (
        200,
        {"job_id": "assess-old", "stories_added": 0, "stories_total": 1},
    )


def test_make_backlog_reads_meta_for_per_repo_epic_and_provenance():
    from dev_team.backlog import BacklogStore

    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-meta/assessment.json", json.dumps(_assessment_payload())
    )
    dash.write_text(
        "audit/assess-meta/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-meta"}),
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.make_backlog("assess-meta")
    assert (status, payload) == (
        200,
        {"job_id": "assess-meta", "stories_added": 1, "stories_total": 1},
    )
    stored = BacklogStore(dash).load()
    # meta.json names the audited repo -> the repo's own epic, and the
    # story can be traced (and re-verified) via source_job + finding_id.
    assert stored.epics[0].title == "Remediation — acme/mono"
    (story,) = stored.stories
    assert story.source_job == "assess-meta"
    assert story.finding_id == "recommendation.plan[0]"


def test_make_backlog_missing_assessment_is_404():
    disp = Dispatcher(token="x", dashboard_workspace=InMemoryWorkspace())
    assert disp.make_backlog("ghost") == (
        404,
        {"error": "no assessment for that job"},
    )


def test_make_backlog_without_dashboard_workspace_is_409():
    assert Dispatcher(token="x").make_backlog("any") == (
        409,
        {"error": "backlog generation needs a dashboard workspace"},
    )


# --- corrupt / traversal-shaped on-disk state answers cleanly, never 500 ------


def test_list_job_findings_corrupt_assessment_is_404():
    dash = InMemoryWorkspace()
    dash.write_text("audit/assess-bad/assessment.json", "{not json")
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    assert disp.list_job_findings("assess-bad") == (
        404, {"error": "no assessment for that job"})


def test_make_backlog_corrupt_assessment_is_404():
    dash = InMemoryWorkspace()
    dash.write_text("audit/assess-bad/assessment.json", "{not json")
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    assert disp.make_backlog("assess-bad") == (
        404, {"error": "no assessment for that job"})


def test_make_backlog_traversal_job_id_is_404():
    # A traversal-shaped id routes through _exists (fails closed) -> 404,
    # instead of raising out of the workspace's path guard as a 500.
    disp = Dispatcher(token="x", dashboard_workspace=InMemoryWorkspace())
    assert disp.make_backlog("../escape") == (
        404, {"error": "no assessment for that job"})


def test_make_backlog_tolerates_corrupt_meta_and_falls_back():
    from dev_team.backlog import BacklogStore

    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-cm/assessment.json", json.dumps(_assessment_payload())
    )
    dash.write_text("audit/assess-cm/meta.json", "{not json")  # unreadable meta
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.make_backlog("assess-cm")
    assert status == 200
    assert payload["stories_added"] == 1
    # a corrupt meta yields no repo -> the shared epic, not a per-repo one
    assert BacklogStore(dash).load().epics[0].title == "Assessment remediation"


def test_verifications_reader_skips_a_corrupt_line():
    dash = _seeded_dash()
    dash.write_text(
        "audit/assess-old/verifications.jsonl",
        '{"finding_id": "a"}\n{not json\n{"finding_id": "b"}\n',
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.verifications("assess-old")
    assert status == 200
    assert [e["finding_id"] for e in payload["verifications"]] == ["a", "b"]


def test_calibration_without_dashboard_workspace_is_409():
    disp = Dispatcher(token="x")
    assert disp.calibration() == (
        409, {"error": "calibration needs a dashboard workspace"})


def test_calibration_with_no_verification_files_is_zeroed():
    disp = Dispatcher(token="x", dashboard_workspace=InMemoryWorkspace())
    status, payload = disp.calibration()
    assert status == 200
    assert payload == {
        "phases": {},
        "overall": {
            "confirmed": 0, "refuted": 0, "needs_context": 0,
            "total": 0, "confirm_rate": None,
        },
        "jobs_counted": 0,
    }


def test_calibration_aggregates_across_multiple_jobs():
    dash = InMemoryWorkspace()
    # a sibling assessment.json (no "/verifications.jsonl" suffix) must be
    # skipped by the file filter, not mistaken for a verdict log
    dash.write_text("audit/assess-a/assessment.json", "{}")
    dash.write_text(
        "audit/assess-a/verifications.jsonl",
        json.dumps({"finding_id": "risk.secrets[0]", "verdict": "confirmed"}) + "\n"
        + "\n"  # a blank line (trailing newline artifact) is tolerated
        + json.dumps({"finding_id": "risk.secrets[1]", "verdict": "refuted"}) + "\n",
    )
    dash.write_text(
        "audit/assess-b/verifications.jsonl",
        json.dumps(
            {"finding_id": "buildability.blockers[0]", "verdict": "confirmed"}
        ) + "\n",
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.calibration()
    assert status == 200
    assert payload["jobs_counted"] == 2
    assert payload["phases"]["risk"]["total"] == 2
    assert payload["phases"]["buildability"]["total"] == 1
    assert payload["overall"]["total"] == 3


def test_calibration_skips_a_corrupt_line_without_crashing():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-old/verifications.jsonl",
        json.dumps({"finding_id": "risk.secrets[0]", "verdict": "confirmed"}) + "\n"
        + "{not json\n"
        + json.dumps({"finding_id": "risk.secrets[1]", "verdict": "refuted"}) + "\n",
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.calibration()
    assert status == 200
    assert payload["jobs_counted"] == 1
    assert payload["phases"]["risk"] == {
        "confirmed": 1, "refuted": 1, "needs_context": 0,
        "total": 2, "confirm_rate": 0.5,
    }


def test_calibration_only_counts_files_that_contributed_a_parseable_line():
    dash = InMemoryWorkspace()
    dash.write_text("audit/assess-empty/verifications.jsonl", "{not json\n")
    dash.write_text(
        "audit/assess-good/verifications.jsonl",
        json.dumps({"finding_id": "risk.secrets[0]", "verdict": "confirmed"}) + "\n",
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.calibration()
    assert status == 200
    assert payload["jobs_counted"] == 1
    assert payload["overall"]["total"] == 1


# --- costs rollup ---------------------------------------------------------


def _insert_job(disp, job_id, mode, state, cost_usd):
    """Register a job directly in the registry, bypassing submit/run_job —
    lets one test exercise every state/cost combination with no real clone
    or agent run."""

    spec = JobSpec(
        mode=mode, repo="acme/mono", title="T", description="D",
        budget_usd=None, id=job_id,
    )
    disp._registry[job_id] = JobRecord(spec=spec, state=state, cost_usd=cost_usd)
    disp._order.append(job_id)


def test_costs_on_empty_registry_is_zeroed():
    disp = Dispatcher(token="x")
    assert disp.costs() == (
        200, {"total_usd": 0.0, "by_mode": {}, "jobs_counted": 0})


def test_costs_counts_one_succeeded_job_without_a_dashboard_workspace():
    # AC2 + AC8: no --dashboard-workspace configured, and never a 409 —
    # archived-exclusion just no-ops via _is_archived's False-when-no-
    # workspace short circuit.
    disp = Dispatcher(token="x")
    _insert_job(disp, "assess-1", "assess", "succeeded", 2.5)
    assert disp.costs() == (
        200, {"total_usd": 2.5, "by_mode": {"assess": 2.5}, "jobs_counted": 1})


def test_costs_sums_across_all_three_modes():
    disp = Dispatcher(token="x")
    _insert_job(disp, "assess-1", "assess", "succeeded", 1.0)
    _insert_job(disp, "deliver-1", "deliver", "succeeded", 2.0)
    _insert_job(disp, "verify-1", "verify", "succeeded", 0.5)
    status, payload = disp.costs()
    assert status == 200
    assert payload["total_usd"] == 3.5
    assert payload["by_mode"] == {"assess": 1.0, "deliver": 2.0, "verify": 0.5}
    assert payload["jobs_counted"] == 3


def test_costs_counts_a_failed_jobs_real_partial_spend():
    # E4/U6: a failed job now carries the real (partial) spend it burned before
    # failing, and /costs must count that toward the rollup — not treat every
    # failed job as $0.
    disp = Dispatcher(token="x")
    _insert_job(disp, "assess-1", "assess", "failed", 1.5)
    assert disp.costs() == (
        200, {"total_usd": 1.5, "by_mode": {"assess": 1.5}, "jobs_counted": 1})


def test_costs_excludes_queued_and_running_jobs():
    disp = Dispatcher(token="x")
    _insert_job(disp, "assess-1", "assess", "queued", None)
    _insert_job(disp, "assess-2", "assess", "running", None)
    assert disp.costs() == (
        200, {"total_usd": 0.0, "by_mode": {}, "jobs_counted": 0})


def test_costs_excludes_a_cancelled_job():
    disp = Dispatcher(token="x")
    _insert_job(disp, "assess-1", "assess", "cancelled", None)
    assert disp.costs() == (
        200, {"total_usd": 0.0, "by_mode": {}, "jobs_counted": 0})


def test_costs_excludes_archived_job_and_reappears_after_unarchive():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-1/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-1"}),
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    _insert_job(disp, "assess-1", "assess", "succeeded", 5.0)

    assert disp.costs() == (
        200, {"total_usd": 5.0, "by_mode": {"assess": 5.0}, "jobs_counted": 1})

    disp.archive_job("assess-1")
    assert disp.costs() == (
        200, {"total_usd": 0.0, "by_mode": {}, "jobs_counted": 0})
    assert disp.costs(include_archived=True) == (
        200, {"total_usd": 5.0, "by_mode": {"assess": 5.0}, "jobs_counted": 1})

    disp.unarchive_job("assess-1")
    assert disp.costs() == (
        200, {"total_usd": 5.0, "by_mode": {"assess": 5.0}, "jobs_counted": 1})


def test_costs_http_route_end_to_end():
    with running(materialise=_mem_materialise) as server:
        assert _call(server, "/costs", token=None) == (
            401, {"error": "unauthorized"})
        status, payload = _call(server, "/costs")
        assert status == 200
        assert payload == {"total_usd": 0.0, "by_mode": {}, "jobs_counted": 0}


def test_costs_http_route_archived_query_param():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-1/meta.json",
        json.dumps(
            {"repo": "acme/mono", "mode": "assess", "id": "assess-1",
             "archived": True}
        ),
    )
    with running(materialise=_mem_materialise, dashboard_workspace=dash) as server:
        _insert_job(server.dispatcher, "assess-1", "assess", "succeeded", 4.0)

        status, payload = _call(server, "/costs")
        assert status == 200
        assert payload["jobs_counted"] == 0

        status, payload = _call(server, "/costs?archived=true")
        assert status == 200
        assert payload["jobs_counted"] == 0  # only the literal "1" reveals archived

        status, payload = _call(server, "/costs?archived=1")
        assert status == 200
        assert payload == {"total_usd": 4.0, "by_mode": {"assess": 4.0}, "jobs_counted": 1}


# --- archive / unarchive (job lifecycle) --------------------------------------


def test_archive_without_dashboard_workspace_is_409():
    disp = Dispatcher(token="x")
    assert disp.archive_job("any") == (
        409, {"error": "archive needs a dashboard workspace"})
    assert disp.unarchive_job("any") == (
        409, {"error": "archive needs a dashboard workspace"})


def test_archive_missing_meta_is_404():
    disp = Dispatcher(token="x", dashboard_workspace=InMemoryWorkspace())
    assert disp.archive_job("ghost") == (
        404, {"error": "no assessment for that job"})
    assert disp.unarchive_job("ghost") == (
        404, {"error": "no assessment for that job"})


def test_archive_corrupt_meta_is_404():
    dash = InMemoryWorkspace()
    dash.write_text("audit/assess-bad/meta.json", "{not json")
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    assert disp.archive_job("assess-bad") == (
        404, {"error": "no assessment for that job"})


def test_archive_traversal_job_id_is_404():
    # A traversal-shaped id fails closed through _exists -> 404, exactly like
    # make_backlog / list_job_findings, never a 500.
    disp = Dispatcher(token="x", dashboard_workspace=InMemoryWorkspace())
    assert disp.archive_job("../../etc/passwd") == (
        404, {"error": "no assessment for that job"})
    assert disp.unarchive_job("../../etc/passwd") == (
        404, {"error": "no assessment for that job"})


def test_archive_and_unarchive_round_trip_the_meta_marker():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-a"}),
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash, clock=lambda: 42.0)

    status, payload = disp.archive_job("assess-a")
    assert (status, payload) == (200, {"id": "assess-a", "archived": True})
    meta = json.loads(dash.read_text("audit/assess-a/meta.json"))
    assert meta["archived"] is True
    assert meta["archived_at"] == 42.0
    assert meta["repo"] == "acme/mono"  # existing fields survive the mutation

    status, payload = disp.unarchive_job("assess-a")
    assert (status, payload) == (200, {"id": "assess-a", "archived": False})
    meta = json.loads(dash.read_text("audit/assess-a/meta.json"))
    assert "archived" not in meta
    assert "archived_at" not in meta


def test_unarchive_a_non_archived_job_is_idempotent():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-b/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-b"}),
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    assert disp.unarchive_job("assess-b") == (
        200, {"id": "assess-b", "archived": False})


def test_archive_refuses_queued_and_running_jobs():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    disp = Dispatcher(
        token="x", runner=_assess_runner(), materialise=materialise,
        dashboard_workspace=InMemoryWorkspace(),
    )
    disp.start()
    try:
        running_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/one"}))
        assert first_in.wait(5)
        queued_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/two"}))
        assert disp.archive_job(running_id) == (409, {"error": "job is running"})
        assert disp.archive_job(queued_id) == (409, {"error": "job is running"})
    finally:
        release.set()
        disp.wait(running_id, 5)
        disp.wait(queued_id, 5)
        disp.stop()


# --- purge (permanent deletion) -------------------------------------------


def _seed_terminal_job(disp, job_id, *, state="succeeded", archived=True):
    """Insert a bare terminal registry record for ``job_id``, bypassing the
    worker — mirrors the on-disk shape ``_mirror_meta``/``_set_archived``
    themselves write, without actually running an assess job."""

    spec = JobSpec(
        mode="assess", repo="acme/mono", title="t", description="",
        budget_usd=None, id=job_id,
    )
    disp._registry[job_id] = JobRecord(spec=spec, state=state)
    disp._order.append(job_id)
    disp._events[job_id] = threading.Event()
    meta = {"repo": "acme/mono", "mode": "assess", "id": job_id}
    if archived:
        meta["archived"] = True
        meta["archived_at"] = 1.0
    disp._dashboard_workspace.write_text(f"audit/{job_id}/meta.json", json.dumps(meta))


def test_purge_unknown_job_is_404():
    disp = Dispatcher(token="x", dashboard_workspace=InMemoryWorkspace())
    assert disp.purge_job("ghost") == (404, {"error": "unknown job"})


def test_purge_queued_and_running_jobs_are_409():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    disp = Dispatcher(
        token="x", runner=_assess_runner(), materialise=materialise,
        dashboard_workspace=InMemoryWorkspace(),
    )
    disp.start()
    try:
        running_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/one"}))
        assert first_in.wait(5)
        queued_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/two"}))
        assert disp.purge_job(running_id) == (409, {"error": "job is running"})
        assert disp.purge_job(queued_id) == (409, {"error": "job is running"})
    finally:
        release.set()
        disp.wait(running_id, 5)
        disp.wait(queued_id, 5)
        disp.stop()


def test_purge_terminal_but_not_archived_is_409(tmp_path):
    disp = Dispatcher(
        token="x", dashboard_workspace=InMemoryWorkspace(), jobs_root=str(tmp_path))
    _seed_terminal_job(disp, "assess-notarchived", archived=False)
    assert disp.purge_job("assess-notarchived") == (
        409, {"error": "job is not archived"})


def test_purge_removes_workspace_audit_files_and_backlog_stories(tmp_path):
    dash = LocalWorkspace(str(tmp_path / "dash"))
    jobs_root = tmp_path / "jobs"
    disp = Dispatcher(token="x", dashboard_workspace=dash, jobs_root=str(jobs_root))
    job_id = "assess-full"
    _seed_terminal_job(disp, job_id, archived=True)

    clone_dir = jobs_root / job_id
    clone_dir.mkdir(parents=True)
    (clone_dir / "marker.txt").write_text("clone contents")
    dash.write_text(f"audit/{job_id}/assessment.md", "# report")
    dash.write_text(f"audit/{job_id}/assessment.json", json.dumps({"success": True}))
    dash.write_text(f"audit/{job_id}/verifications.jsonl", "{}\n")

    store = BacklogStore(dash)
    backlog = store.load()
    backlog.add_story("finding A", source_job=job_id)
    backlog.add_story("finding B", source_job=job_id)
    other = backlog.add_story("unrelated", source_job="other-job")
    store.save(backlog)

    status, payload = disp.purge_job(job_id)
    assert (status, payload) == (
        200,
        {
            "id": job_id,
            "purged": True,
            "removed": {"workspace": True, "audit": True, "backlog_stories": 2},
        },
    )
    assert not clone_dir.exists()
    for name in ("assessment.md", "assessment.json", "meta.json", "verifications.jsonl"):
        assert not dash.exists(f"audit/{job_id}/{name}")
    survivors = store.load().stories
    assert [s.id for s in survivors] == [other.id]
    assert disp.get(job_id) is None


def test_purge_missing_workspace_dir_reports_removed_false_without_raising(tmp_path):
    dash = LocalWorkspace(str(tmp_path / "dash"))
    disp = Dispatcher(
        token="x", dashboard_workspace=dash, jobs_root=str(tmp_path / "jobs"))
    job_id = "assess-nodir"
    _seed_terminal_job(disp, job_id, archived=True)  # no clone dir ever created

    status, payload = disp.purge_job(job_id)
    assert status == 200
    assert payload["removed"]["workspace"] is False


def test_purge_partial_audit_files_removed_without_raising(tmp_path):
    dash = LocalWorkspace(str(tmp_path / "dash"))
    disp = Dispatcher(
        token="x", dashboard_workspace=dash, jobs_root=str(tmp_path / "jobs"))
    job_id = "assess-partial"
    # only meta.json exists (via _seed_terminal_job); assessment.md/json and
    # verifications.jsonl are already absent.
    _seed_terminal_job(disp, job_id, archived=True)

    status, payload = disp.purge_job(job_id)
    assert status == 200
    assert payload["removed"]["audit"] is True  # meta.json existed and was removed
    assert not dash.exists(f"audit/{job_id}/meta.json")


def test_purge_no_matching_backlog_stories_is_zero(tmp_path):
    dash = LocalWorkspace(str(tmp_path / "dash"))
    disp = Dispatcher(
        token="x", dashboard_workspace=dash, jobs_root=str(tmp_path / "jobs"))
    job_id = "assess-nostories"
    _seed_terminal_job(disp, job_id, archived=True)
    store = BacklogStore(dash)
    backlog = store.load()
    backlog.add_story("unrelated", source_job="other-job")
    store.save(backlog)

    status, payload = disp.purge_job(job_id)
    assert status == 200
    assert payload["removed"]["backlog_stories"] == 0
    assert len(store.load().stories) == 1  # the unrelated story survives


def test_purge_is_not_idempotent_second_call_is_404(tmp_path):
    dash = LocalWorkspace(str(tmp_path / "dash"))
    disp = Dispatcher(
        token="x", dashboard_workspace=dash, jobs_root=str(tmp_path / "jobs"))
    job_id = "assess-twice"
    _seed_terminal_job(disp, job_id, archived=True)

    assert disp.purge_job(job_id)[0] == 200
    assert disp.purge_job(job_id) == (404, {"error": "unknown job"})


class _SpyWorkspace:
    """Wraps a real :class:`Workspace`, recording every ``delete`` call.

    Exposes no raw filesystem primitives itself — the whole point is proving
    a caller only ever removes files through :meth:`Workspace.delete`, never
    a raw ``os``/``pathlib``/``shutil`` call against the wrapped root.
    """

    def __init__(self, inner):
        self.inner = inner
        self.deleted = []

    def read_text(self, path):
        return self.inner.read_text(path)

    def write_text(self, path, content):
        self.inner.write_text(path, content)

    def exists(self, path):
        return self.inner.exists(path)

    def delete(self, path):
        self.deleted.append(path)
        self.inner.delete(path)

    def list_files(self):
        return self.inner.list_files()


def test_purge_audit_deletion_never_touches_the_dashboard_root_directly(
    tmp_path, monkeypatch
):
    # SECURITY: the audit/<id>/ mirror must be removed exclusively through
    # Workspace.delete (the traversal/symlink-escape-checked abstraction
    # every other route uses) -- never shutil.rmtree or a raw fs call
    # against the dashboard workspace's own root.
    real = LocalWorkspace(str(tmp_path / "dash"))
    spy = _SpyWorkspace(real)
    jobs_root = tmp_path / "jobs"
    disp = Dispatcher(token="x", dashboard_workspace=spy, jobs_root=str(jobs_root))
    job_id = "assess-spy"
    _seed_terminal_job(disp, job_id, archived=True)  # writes meta.json via spy
    real.write_text(f"audit/{job_id}/assessment.md", "# report")
    real.write_text(f"audit/{job_id}/assessment.json", "{}")
    real.write_text(f"audit/{job_id}/verifications.jsonl", "{}\n")
    (jobs_root / job_id).mkdir(parents=True)

    rmtree_calls = []
    monkeypatch.setattr(
        dispatch_mod.shutil, "rmtree",
        lambda path, ignore_errors=False: rmtree_calls.append(str(path)),
    )

    status, _ = disp.purge_job(job_id)
    assert status == 200
    # rmtree is used exactly once, and only for the job's own clone dir --
    # never anywhere under the dashboard workspace's root.
    assert rmtree_calls == [str(jobs_root / job_id)]
    assert set(spy.deleted) == {
        f"audit/{job_id}/{name}" for name in
        ("assessment.md", "assessment.json", "meta.json", "verifications.jsonl")
    }


def test_purge_refuses_a_symlink_escape_in_the_audit_mirror(tmp_path):
    # SECURITY: a symlink planted at one of the four audit/<id>/ paths,
    # pointing outside the dashboard workspace root, must be refused by
    # Workspace's own _within_root check -- never silently followed.
    dash_root = tmp_path / "dash"
    dash = LocalWorkspace(str(dash_root))
    disp = Dispatcher(
        token="x", dashboard_workspace=dash, jobs_root=str(tmp_path / "jobs"))
    job_id = "assess-symlink"
    _seed_terminal_job(disp, job_id, archived=True)

    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not delete me")
    audit_dir = dash_root / "audit" / job_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "assessment.json").symlink_to(outside)

    status, payload = disp.purge_job(job_id)
    assert status == 200  # the rest of the purge still completes
    assert payload["purged"] is True
    assert outside.exists()
    assert outside.read_text() == "do not delete me"


def test_purge_never_reenters_the_registry_lock(tmp_path, monkeypatch):
    """SECURITY/CORRECTNESS: purge_job must never call _job_running -- which
    itself acquires self._lock -- while already holding self._lock, or the
    calling thread (and, because every mutation shares this lock, the whole
    single-flight dispatcher) deadlocks forever. Patch _job_running to blow
    up if invoked at all, and prove every branch (unknown / running /
    not-archived / success) returns promptly from a background thread -- a
    real deadlock would hang the join forever.
    """

    def _boom(self, job_id):
        raise AssertionError("purge_job must never call _job_running")

    monkeypatch.setattr(Dispatcher, "_job_running", _boom)

    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    disp = Dispatcher(
        token="x", runner=_assess_runner(), materialise=materialise,
        dashboard_workspace=InMemoryWorkspace(), jobs_root=str(tmp_path),
    )
    disp.start()
    results = {}

    def call(name, job_id):
        results[name] = disp.purge_job(job_id)

    try:
        running_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/one"}))
        assert first_in.wait(5)

        t = threading.Thread(target=call, args=("unknown", "ghost"))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "purge_job(unknown) hung -- possible deadlock"
        assert results["unknown"] == (404, {"error": "unknown job"})

        t = threading.Thread(target=call, args=("running", running_id))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "purge_job(running) hung -- possible deadlock"
        assert results["running"] == (409, {"error": "job is running"})
    finally:
        release.set()
        disp.wait(running_id, 5)
        disp.stop()

    _seed_terminal_job(disp, "assess-na", archived=False)
    t = threading.Thread(target=call, args=("not_archived", "assess-na"))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "purge_job(not archived) hung -- possible deadlock"
    assert results["not_archived"] == (409, {"error": "job is not archived"})

    _seed_terminal_job(disp, "assess-ok", archived=True)
    t = threading.Thread(target=call, args=("success", "assess-ok"))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "purge_job(success) hung -- possible deadlock"
    assert results["success"][0] == 200


def test_purge_backlog_removal_shares_the_lock_with_concurrent_writes(tmp_path):
    """A purge's backlog-story removal and a concurrent POST /backlog/story
    (add_story_card) share self._backlog_lock, so they cannot interleave
    into a corrupt backlog.json -- the same guarantee
    test_backlog_write_lock_prevents_lost_updates already proves for
    add_story_card + _merge_backlog.
    """

    dash = InMemoryWorkspace()

    def slow_clock():
        time.sleep(0.002)  # widen the load->save window the lock must cover
        return time.time()

    disp = Dispatcher(
        token="x", dashboard_workspace=dash, clock=slow_clock,
        jobs_root=str(tmp_path),
    )
    job_id = "assess-lockrace"
    _seed_terminal_job(disp, job_id, archived=True)
    store = BacklogStore(dash)
    backlog = store.load()
    for _ in range(5):
        backlog.add_story("to purge", source_job=job_id)
    store.save(backlog)

    per_thread = 10

    def add_cards():
        for index in range(per_thread):
            assert disp.add_story_card({"title": f"new-{index}"})[0] == 201

    results = {}

    def purge():
        results["purge"] = disp.purge_job(job_id)

    threads = [
        threading.Thread(target=add_cards),
        threading.Thread(target=add_cards),
        threading.Thread(target=purge),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert results["purge"][0] == 200
    assert results["purge"][1]["removed"]["backlog_stories"] == 5

    status, board = disp.board()
    assert status == 200
    ids = [s["id"] for s in board["stories"]]
    assert len(set(ids)) == len(ids)  # no corruption / duplicate ids under the race
    assert len(board["stories"]) == 2 * per_thread  # the 5 purged stories are gone


# --- cancel (job lifecycle) ----------------------------------------------


def test_cancel_a_queued_job_transitions_to_cancelled():
    disp = Dispatcher(token="x", clock=lambda: 42.0)  # worker never started
    job_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
    assert disp.cancel_job(job_id) == (200, {"id": job_id, "state": "cancelled"})
    record = disp.get(job_id)
    assert record.state == "cancelled"
    assert record.ended == 42.0


def test_cancel_unknown_job_is_404():
    disp = Dispatcher(token="x")
    assert disp.cancel_job("ghost") == (404, {"error": "unknown job"})


def test_cancel_a_running_job_is_409():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    disp = Dispatcher(token="x", runner=_assess_runner(), materialise=materialise)
    disp.start()
    try:
        running_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/one"}))
        assert first_in.wait(5)
        assert disp.cancel_job(running_id) == (
            409, {"error": "job is not queued", "state": "running"})
    finally:
        release.set()
        disp.wait(running_id, 5)
        disp.stop()


def test_cancel_an_already_terminal_job_is_409_per_state():
    def boom(spec, dest):
        raise RuntimeError("boom")

    disp = Dispatcher(token="x", runner=_assess_runner(), materialise=_mem_materialise)
    disp.start()
    try:
        succeeded_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/one"}))
        disp.wait(succeeded_id, 5)
        assert disp.get(succeeded_id).state == "succeeded"
        assert disp.cancel_job(succeeded_id) == (
            409, {"error": "job is not queued", "state": "succeeded"})
    finally:
        disp.stop()

    disp = Dispatcher(token="x", materialise=boom)
    disp.start()
    try:
        failed_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/two"}))
        disp.wait(failed_id, 5)
        assert disp.get(failed_id).state == "failed"
        assert disp.cancel_job(failed_id) == (
            409, {"error": "job is not queued", "state": "failed"})
    finally:
        disp.stop()

    disp = Dispatcher(token="x")  # worker never started
    cancelled_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/three"}))
    disp.cancel_job(cancelled_id)
    assert disp.cancel_job(cancelled_id) == (
        409, {"error": "job is not queued", "state": "cancelled"})


def test_cancelled_job_is_never_executed():
    calls = []
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        calls.append(spec.id)
        if len(calls) == 1:
            first_in.set()
            release.wait(5)
        return InMemoryWorkspace()

    disp = Dispatcher(token="x", runner=_assess_runner(), materialise=materialise)
    disp.start()
    try:
        running_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/one"}))
        assert first_in.wait(5)
        queued_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/two"}))
        assert disp.get(queued_id).state == "queued"
        assert disp.cancel_job(queued_id) == (
            200, {"id": queued_id, "state": "cancelled"})
        release.set()
        disp.wait(running_id, 5)
        disp.wait(queued_id, 5)
        assert queued_id not in calls  # materialise never called for it
        assert disp.get(queued_id).state == "cancelled"  # not overwritten by _execute
    finally:
        release.set()
        disp.stop()


def test_cancel_running_race_is_deterministic_and_consistent():
    """AC11: cancel_job() and _execute()'s queued->running flip share
    self._lock, so whichever wins a race resolves to exactly one outcome —
    either the job never ran (materialise never called, state stays
    "cancelled"), or cancel is refused with 409 because the job is already
    past "queued" — never both, never neither.
    """

    calls = []
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        calls.append(spec.id)
        if len(calls) == 1:
            first_in.set()
            release.wait(5)
        return InMemoryWorkspace()

    disp = Dispatcher(token="x", runner=_assess_runner(), materialise=materialise)
    disp.start()
    try:
        running_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/one"}))
        assert first_in.wait(5)
        queued_id, _ = disp.submit(
            disp.build_spec({"mode": "assess", "repo": "a/two"}))
        release.set()  # free the worker to race straight for job 2
        status, payload = disp.cancel_job(queued_id)
        disp.wait(queued_id, 5)
        record = disp.get(queued_id)
        if status == 200:
            assert payload == {"id": queued_id, "state": "cancelled"}
            assert record.state == "cancelled"
            assert queued_id not in calls
        else:
            assert status == 409
            assert payload["error"] == "job is not queued"
            assert record.state in ("running", "succeeded")
    finally:
        disp.wait(running_id, 5)
        disp.stop()


def test_cancel_result_and_status_shape():
    disp = Dispatcher(token="x", clock=lambda: 7.0)  # worker never started
    job_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
    disp.cancel_job(job_id)
    record = disp.get(job_id)
    status_payload = disp.status(record)
    assert status_payload["state"] == "cancelled"
    assert status_payload["ended"] == 7.0
    assert status_payload["cost_usd"] is None
    assert status_payload["error"] is None
    assert disp.result(record) == (
        200,
        {"kind": "assess", "success": False, "error": "cancelled", "cost_usd": 0},
    )


def test_cancelled_job_is_listed_but_not_archivable():
    dash = InMemoryWorkspace()
    disp = Dispatcher(token="x", dashboard_workspace=dash)  # worker never started
    job_id, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
    disp.cancel_job(job_id)
    assert [r.spec.id for r in disp.recent()] == [job_id]
    # A cancelled job is cancelled before run_job ever runs, so its
    # audit/<id>/meta.json is never written — archive_job 404s exactly like
    # it already does for any job whose assessment was never mirrored.
    assert disp.archive_job(job_id) == (404, {"error": "no assessment for that job"})


def test_cancel_frees_a_queue_cap_slot():
    disp = Dispatcher(token="x", queue_cap=2)  # worker never started
    id1, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
    disp.submit(disp.build_spec({"mode": "assess", "repo": "a/two"}))
    with pytest.raises(QueueFull):
        disp.submit(disp.build_spec({"mode": "assess", "repo": "a/three"}))
    disp.cancel_job(id1)
    id3, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/three"}))
    assert disp.get(id3).state == "queued"


def test_recent_excludes_archived_jobs_by_default():
    dash = InMemoryWorkspace()
    disp = Dispatcher(
        token="x", runner=_assess_runner(), materialise=_mem_materialise,
        dashboard_workspace=dash,
    )
    disp.start()
    try:
        id1, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
        disp.wait(id1, 5)
        id2, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/two"}))
        disp.wait(id2, 5)
        disp.archive_job(id1)
        assert [r.spec.id for r in disp.recent()] == [id2]
        assert {r.spec.id for r in disp.recent(include_archived=True)} == {id1, id2}
        disp.unarchive_job(id1)
        assert {r.spec.id for r in disp.recent()} == {id1, id2}
    finally:
        disp.stop()


def test_calibration_excludes_archived_job_and_reappears_after_unarchive():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-a/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-a"}),
    )
    dash.write_text(
        "audit/assess-a/verifications.jsonl",
        json.dumps({"finding_id": "risk.secrets[0]", "verdict": "confirmed"}) + "\n",
    )
    dash.write_text(
        "audit/assess-b/verifications.jsonl",
        json.dumps({"finding_id": "risk.secrets[1]", "verdict": "refuted"}) + "\n",
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)

    status, payload = disp.calibration()
    assert status == 200
    assert payload["jobs_counted"] == 2
    assert payload["overall"]["total"] == 2

    disp.archive_job("assess-a")
    status, payload = disp.calibration()
    assert status == 200
    assert payload["jobs_counted"] == 1
    assert payload["overall"]["total"] == 1
    assert payload["overall"]["refuted"] == 1  # only assess-b's verdict survives

    disp.unarchive_job("assess-a")
    status, payload = disp.calibration()
    assert payload["jobs_counted"] == 2
    assert payload["overall"]["total"] == 2


def test_calibration_http_route_end_to_end():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-a/verifications.jsonl",
        json.dumps({"finding_id": "risk.secrets[0]", "verdict": "confirmed"}) + "\n",
    )
    with running(materialise=_mem_materialise, dashboard_workspace=dash) as server:
        assert _call(server, "/calibration", token=None) == (
            401, {"error": "unauthorized"})
        status, payload = _call(server, "/calibration")
        assert status == 200
        assert set(payload) == {"phases", "overall", "jobs_counted"}
        assert payload["jobs_counted"] == 1


def test_archive_and_unarchive_http_routes_round_trip():
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-http/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-http"}),
    )
    with running(materialise=_mem_materialise, dashboard_workspace=dash) as server:
        assert _call(server, "/jobs/assess-http/archive", method="POST") == (
            200, {"id": "assess-http", "archived": True})
        meta = json.loads(dash.read_text("audit/assess-http/meta.json"))
        assert meta["archived"] is True
        assert _call(server, "/jobs/assess-http/unarchive", method="POST") == (
            200, {"id": "assess-http", "archived": False})
        meta = json.loads(dash.read_text("audit/assess-http/meta.json"))
        assert "archived" not in meta


def test_archive_and_unarchive_http_routes_require_auth():
    # SECURITY: unauthenticated archive/unarchive answers 401, matching
    # every other dispatch route.
    with running(materialise=_mem_materialise) as server:
        assert _call(server, "/jobs/x/archive", method="POST", token=None) == (
            401, {"error": "unauthorized"})
        assert _call(server, "/jobs/x/unarchive", method="POST", token=None) == (
            401, {"error": "unauthorized"})


def test_archive_http_route_traversal_id_is_404():
    # SECURITY: a raw ".." segment splits the URL into more path components
    # than the {id}/archive route shape expects, so the router itself
    # rejects it (generic 404) before any workspace path is ever built.
    with running(materialise=_mem_materialise,
                 dashboard_workspace=InMemoryWorkspace()) as server:
        status, payload = _call(
            server, "/jobs/../../etc/passwd/archive", method="POST")
        assert (status, payload) == (404, {"error": "not found"})
        # A traversal-shaped id with no raw "/" (percent-encoded, so it
        # survives as ONE path segment) reaches Dispatcher.archive_job,
        # which fails closed via _exists — see
        # test_archive_traversal_job_id_is_404 for that same guarantee
        # exercised directly against the core.
        status, payload = _call(
            server, "/jobs/%2e%2e%2Fetc%2Fpasswd/archive", method="POST")
        assert (status, payload) == (404, {"error": "no assessment for that job"})


def test_archive_http_route_running_job_is_409():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    with running(runner=_assess_runner(), materialise=materialise,
                 dashboard_workspace=InMemoryWorkspace()) as server:
        _, job = _call(server, "/jobs", method="POST",
                       body={"mode": "assess", "repo": "a/one"})
        assert first_in.wait(5)
        assert _call(server, f"/jobs/{job['id']}/archive", method="POST") == (
            409, {"error": "job is running"})
        release.set()
        server.dispatcher.wait(job["id"], 5)


def test_purge_http_route_round_trips(tmp_path):
    jobs_root = tmp_path / "jobs"

    def materialise(spec, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        # A real clone is never empty — seed a file so the assess engine's
        # empty-workspace guard does not fail the job.
        (Path(dest) / "README.md").write_text("# repo\n")
        return LocalWorkspace(dest)

    with running(runner=_assess_runner(), materialise=materialise,
                 dashboard_workspace=InMemoryWorkspace(),
                 jobs_root=str(jobs_root)) as server:
        _, job = _call(server, "/jobs", method="POST",
                       body={"mode": "assess", "repo": "a/one"})
        job_id = job["id"]
        assert server.dispatcher.wait(job_id, 5)
        assert _call(server, f"/jobs/{job_id}/archive", method="POST") == (
            200, {"id": job_id, "archived": True})

        status, payload = _call(server, f"/jobs/{job_id}/purge", method="POST")
        assert status == 200
        assert payload["id"] == job_id
        assert payload["purged"] is True
        assert payload["removed"]["workspace"] is True
        assert not (jobs_root / job_id).exists()
        assert _call(server, f"/jobs/{job_id}") == (404, {"error": "unknown job"})
        # not idempotent: a second purge answers 404, never 200 again
        assert _call(server, f"/jobs/{job_id}/purge", method="POST") == (
            404, {"error": "unknown job"})


def test_purge_http_route_requires_auth():
    # SECURITY: unauthenticated purge answers 401, matching every other
    # authenticated dispatch route.
    with running(materialise=_mem_materialise) as server:
        assert _call(server, "/jobs/x/purge", method="POST", token=None) == (
            401, {"error": "unauthorized"})


def test_purge_http_route_traversal_id_is_404():
    # SECURITY: a raw ".." segment splits the URL into more path components
    # than the {id}/purge route shape expects, so the router itself rejects
    # it (generic 404) before Dispatcher.purge_job ever runs.
    with running(materialise=_mem_materialise,
                 dashboard_workspace=InMemoryWorkspace()) as server:
        status, payload = _call(
            server, "/jobs/../../etc/passwd/purge", method="POST")
        assert (status, payload) == (404, {"error": "not found"})
        # A traversal-shaped id with no raw "/" (percent-encoded, so it
        # survives as ONE path segment) reaches Dispatcher.purge_job, which
        # is never a known registry id -> a clean 404, never reaching the
        # delete step (no file outside jobs_root/<id> or audit/<id>/ is ever
        # touched, since purge_job returns before any deletion).
        status, payload = _call(
            server, "/jobs/%2e%2e%2Fetc%2Fpasswd/purge", method="POST")
        assert (status, payload) == (404, {"error": "unknown job"})


def test_cancel_http_route_blocks_a_still_queued_job():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    with running(runner=_assess_runner(), materialise=materialise) as server:
        _, running_job = _call(server, "/jobs", method="POST",
                               body={"mode": "assess", "repo": "a/one"})
        assert first_in.wait(5)
        _, queued_job = _call(server, "/jobs", method="POST",
                              body={"mode": "assess", "repo": "a/two"})
        assert _call(server, f"/jobs/{queued_job['id']}/cancel", method="POST") == (
            200, {"id": queued_job["id"], "state": "cancelled"})
        assert _call(server, f"/jobs/{queued_job['id']}/result") == (
            200,
            {"kind": "assess", "success": False, "error": "cancelled", "cost_usd": 0},
        )
        release.set()
        server.dispatcher.wait(running_job["id"], 5)


def test_cancel_http_route_unknown_and_running_and_double_cancel():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return _clone_ws()

    with running(runner=_assess_runner(), materialise=materialise) as server:
        assert _call(server, "/jobs/ghost/cancel", method="POST") == (
            404, {"error": "unknown job"})

        _, job = _call(server, "/jobs", method="POST",
                       body={"mode": "assess", "repo": "a/one"})
        assert first_in.wait(5)
        assert _call(server, f"/jobs/{job['id']}/cancel", method="POST") == (
            409, {"error": "job is not queued", "state": "running"})
        release.set()
        server.dispatcher.wait(job["id"], 5)
        # already succeeded -> still refused, not idempotent
        assert _call(server, f"/jobs/{job['id']}/cancel", method="POST") == (
            409, {"error": "job is not queued", "state": "succeeded"})


def test_cancel_http_route_requires_auth():
    # SECURITY: unauthenticated cancel answers 401, matching every other
    # authenticated dispatch route.
    with running(materialise=_mem_materialise) as server:
        assert _call(server, "/jobs/x/cancel", method="POST", token=None) == (
            401, {"error": "unauthorized"})


# --- interactive dispatch: question/answer (issue #87) -----------------------


def _sample_question():
    return Question(
        topic="plan-review",
        prompt="Approve?",
        choices=(Choice("approve", "go"), Choice("abort", "stop")),
        context="plan text",
        asked_by="Priya",
    )


def _interactive_spec(job_id, **overrides):
    fields = dict(
        mode="deliver", repo="acme/mono", title="T", description="D",
        budget_usd=None, interactive=True, id=job_id,
    )
    fields.update(overrides)
    return JobSpec(**fields)


def _run_interactive(disp, spec):
    """Run an interactive deliver job's run_job() to completion on its own
    thread (with its own event loop), registering it in disp's registry
    first so answer_question()/get_question() can find it by id. Returns
    (record, thread, box); the caller must service record.channel (read the
    posted Question off record.channel.questions, answer via
    disp.answer_question) and then join the thread. ``box`` collects
    ``outcome``/``cost`` on success or ``error`` on an uncaught exception.
    """

    record = JobRecord(spec=spec)
    disp._registry[spec.id] = record
    box = {}

    def target():
        try:
            box["outcome"], box["cost"] = asyncio.run(disp.run_job(record))
        except Exception as exc:  # noqa: BLE001 - surfaced to the test via box
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return record, thread, box


def _await_channel(record, timeout=5.0):
    """Wait for run_job to construct record.channel (near-instant: it
    happens synchronously before the first await in run_job, but the
    calling thread still needs to observe it)."""

    deadline = time.time() + timeout
    while record.channel is None:
        if time.time() > deadline:
            raise AssertionError("interactive channel was never constructed")
        time.sleep(0.005)
    return record.channel


def _await_pending(channel, timeout=5.0):
    """Wait for a background ``channel.ask()`` call to publish its question
    (near-instant: :meth:`_TrackedChannel.ask` sets it before blocking, but
    the calling thread still needs to observe it)."""

    deadline = time.time() + timeout
    while channel.current is None:
        if time.time() > deadline:
            raise AssertionError("ask() never posted a question")
        time.sleep(0.005)


def test_build_spec_interactive_defaults_false_and_must_be_bool():
    disp = Dispatcher(token="x")
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "a/b", "title": "T", "description": "D"}
    )
    assert spec.interactive is False
    assert spec.interactive_timeout_seconds is None

    spec = disp.build_spec(
        {"mode": "deliver", "repo": "a/b", "title": "T", "description": "D",
         "interactive": True, "interactive_timeout_seconds": 60}
    )
    assert spec.interactive is True
    assert spec.interactive_timeout_seconds == 60

    for bad in (1, 0, "true", None, [], {}):
        with pytest.raises(ValidationError):
            disp.build_spec({"mode": "assess", "repo": "a/b", "interactive": bad})
    for bad in (True, "60", [], {}):
        with pytest.raises(ValidationError):
            disp.build_spec(
                {"mode": "assess", "repo": "a/b", "interactive_timeout_seconds": bad}
            )
    # null is explicitly allowed (means "use the default once running")
    assert disp.build_spec(
        {"mode": "assess", "repo": "a/b", "interactive_timeout_seconds": None}
    ).interactive_timeout_seconds is None


def test_resolved_interactive_timeout_defaults_and_clamps():
    assert dispatch_mod._resolved_interactive_timeout(None) == 300.0
    assert dispatch_mod._resolved_interactive_timeout(5) == 30.0
    assert dispatch_mod._resolved_interactive_timeout(999999) == 1800.0
    assert dispatch_mod._resolved_interactive_timeout(600) == 600.0


def test_run_job_deliver_non_interactive_baseline_is_unchanged(monkeypatch):
    # AC4 [baseline]: interactive absent (default False) -> zero
    # _TrackedChannel construction and interaction=None still passed to
    # DevTeam(...), exactly like before this feature existed.
    captured = {}
    real_init = dispatch_mod.DevTeam.__init__

    def spy_init(self, *args, **kwargs):
        captured["interaction"] = kwargs.get("interaction")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(dispatch_mod.DevTeam, "__init__", spy_init)

    disp = Dispatcher(token="x", runner=_deliver_runner(), materialise=_mem_materialise)
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d"}
    )
    spec.id = "deliver-baseline"
    record = JobRecord(spec=spec)
    outcome, cost = asyncio.run(disp.run_job(record))
    assert outcome.success is True
    assert record.channel is None
    assert captured["interaction"] is None


def test_interactive_deliver_default_timeout_resolves_to_300_once_running():
    # AC1: interactive: true with no interactive_timeout_seconds resolves to
    # the documented default (300) once running, asserted via the record.
    disp = Dispatcher(token="x", runner=_deliver_runner(), materialise=_mem_materialise)
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d",
         "interactive": True}
    )
    spec.id = "deliver-ac1"
    record, thread, box = _run_interactive(disp, spec)
    try:
        channel = _await_channel(record)
        assert channel.timeout == 300.0
        question = channel.questions.get(timeout=5)
        assert question.topic == "plan-review"
        status, payload = disp.answer_question(spec.id, {"choice": question.default.key})
        assert status == 202
    finally:
        thread.join(timeout=5)
    assert "error" not in box
    assert box["outcome"].success is True


@pytest.mark.parametrize("requested,expected", [(5, 30.0), (999999, 1800.0)])
def test_interactive_deliver_clamps_out_of_range_timeout(requested, expected):
    # AC2 [security/resource-bounding]: out-of-bound requested timeouts are
    # clamped to the floor/ceiling rather than stored/used as-is — asserted
    # against what _TrackedChannel is actually constructed with.
    disp = Dispatcher(token="x", runner=_deliver_runner(), materialise=_mem_materialise)
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d",
         "interactive": True, "interactive_timeout_seconds": requested}
    )
    spec.id = f"deliver-clamp-{requested}"
    record, thread, box = _run_interactive(disp, spec)
    try:
        channel = _await_channel(record)
        assert channel.timeout == expected
        question = channel.questions.get(timeout=5)
        status, _ = disp.answer_question(spec.id, {"choice": question.default.key})
        assert status == 202
    finally:
        thread.join(timeout=5)
    assert "error" not in box


def test_interactive_deliver_end_to_end_preserves_approval_gate(monkeypatch):
    # AC7 [security]: through a REAL (not mocked) DeliveryEngine plan-review
    # pause, the deliver job's approval gate is still the unchanged
    # PolicyApprovalGate(block_risks=("high",)) instance/config — answering
    # interactive questions cannot be used to approve a high-risk command.
    captured = {}
    real_make_engine = dispatch_mod.DevTeam.make_engine

    def spy_make_engine(self, **kwargs):
        engine = real_make_engine(self, **kwargs)
        captured["approval"] = kwargs.get("approval")
        return engine

    monkeypatch.setattr(dispatch_mod.DevTeam, "make_engine", spy_make_engine)

    disp = Dispatcher(token="x", runner=_deliver_runner(), materialise=_mem_materialise)
    spec = disp.build_spec(
        {"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d",
         "interactive": True}
    )
    spec.id = "deliver-ac7"
    record, thread, box = _run_interactive(disp, spec)
    try:
        channel = _await_channel(record)
        question = channel.questions.get(timeout=5)
        assert question.topic == "plan-review"
        # A separate assertion (not just the approval gate) that answering
        # the question has no code path touching record.spec at all.
        spec_before = dict(vars(record.spec))
        status, _ = disp.answer_question(spec.id, {"choice": question.default.key})
        assert status == 202
        assert dict(vars(record.spec)) == spec_before
    finally:
        thread.join(timeout=5)
    assert "error" not in box
    assert box["outcome"].success is True

    approval = captured["approval"]
    assert isinstance(approval, PolicyApprovalGate)
    assert approval.block_risks == ("high",)


def test_answer_question_rejects_unknown_choice_without_unblocking():
    # AC5 [security]: an invalid choice must not push anything onto
    # replies — ask() (driven for real, on a background thread) is still
    # blocked afterward; a valid choice then unblocks it promptly.
    disp = Dispatcher(token="x")
    channel = dispatch_mod._TrackedChannel(timeout=5)
    spec = _interactive_spec("ans-1")
    disp._registry[spec.id] = JobRecord(spec=spec, channel=channel)

    result = {}

    def ask():
        result["reply"] = channel.ask(_sample_question())

    thread = threading.Thread(target=ask)
    thread.start()
    _await_pending(channel)

    assert disp.answer_question(spec.id, {"choice": "nope"}) == (
        400, {"error": "unknown choice"})
    time.sleep(0.05)
    assert thread.is_alive(), "an invalid choice must not have unblocked ask()"

    assert disp.answer_question(spec.id, {"choice": "approve"}) == (202, {})
    thread.join(timeout=5)
    assert result["reply"] == Reply(choice="approve", text="")


def test_answer_question_non_string_choice_is_400():
    disp = Dispatcher(token="x")
    channel = dispatch_mod._TrackedChannel(timeout=5)
    channel.current = _sample_question()
    spec = _interactive_spec("ans-nc")
    disp._registry[spec.id] = JobRecord(spec=spec, channel=channel)
    assert disp.answer_question(spec.id, {"choice": None}) == (
        400, {"error": "unknown choice"})
    assert disp.answer_question(spec.id, {}) == (400, {"error": "unknown choice"})


def test_answer_question_defaults_text_and_coerces_non_string():
    # Drives real (threaded) ask() calls rather than poking channel.current
    # directly: replies are now delivered to the single-use slot ask()
    # itself minted (see _TrackedChannel), not a shared queue, so the only
    # way to observe a delivered Reply is via the ask() call it belongs to.
    disp = Dispatcher(token="x")
    channel = dispatch_mod._TrackedChannel(timeout=5)
    spec = _interactive_spec("ans-txt")
    disp._registry[spec.id] = JobRecord(spec=spec, channel=channel)

    box = {}

    def ask():
        box["reply"] = channel.ask(_sample_question())

    thread = threading.Thread(target=ask)
    thread.start()
    _await_pending(channel)
    assert disp.answer_question(spec.id, {"choice": "approve"}) == (202, {})
    thread.join(timeout=5)
    assert box["reply"] == Reply(choice="approve", text="")

    thread = threading.Thread(target=ask)
    thread.start()
    _await_pending(channel)
    assert disp.answer_question(
        spec.id, {"choice": "approve", "text": 123}
    ) == (202, {})
    thread.join(timeout=5)
    assert box["reply"] == Reply(choice="approve", text="")


def test_answer_cannot_be_misdelivered_to_a_later_unrelated_question():
    # Regression for the PR #89 review finding: a reply meant for one
    # question must never resolve a *different* question the channel has
    # since moved on to (e.g. after the first ask() timed out and took its
    # default), even though both ask() calls share the same _TrackedChannel.
    # The old implementation reused one `QueueChannel.replies` queue for the
    # channel's whole lifetime, so any Reply pushed onto it was blindly
    # consumed by whichever ask() call happened to be blocked next -
    # regardless of whether the choice was even valid for that question.
    disp = Dispatcher(token="x")
    channel = dispatch_mod._TrackedChannel(timeout=0.05)
    spec = _interactive_spec("race-1")
    disp._registry[spec.id] = JobRecord(spec=spec, channel=channel)

    question_a = Question(
        topic="task-failure",
        prompt="Task failed, what now?",
        choices=(Choice("skip", "skip it"),
                 Choice("retry", "retry it", accepts_text=True)),
        context="boom",
        asked_by="Priya",
    )
    box_a = {}
    thread_a = threading.Thread(
        target=lambda: box_a.__setitem__("reply", channel.ask(question_a))
    )
    thread_a.start()
    _await_pending(channel)
    thread_a.join(timeout=5)
    assert not thread_a.is_alive()
    assert box_a["reply"] == Reply(choice="skip")  # timed out -> default taken
    assert channel._pending is None

    question_b = Question(
        topic="plan-review",
        prompt="Approve this plan?",
        choices=(Choice("approve", "go"),
                 Choice("revise", "change it", accepts_text=True),
                 Choice("abort", "stop")),
        context="plan",
        asked_by="Priya",
    )
    channel.timeout = 5  # give the test room to interact with question B
    box_b = {}
    thread_b = threading.Thread(
        target=lambda: box_b.__setitem__("reply", channel.ask(question_b))
    )
    thread_b.start()
    _await_pending(channel)

    # A choice that was only ever meant for (and valid on) question A must
    # not leak through to resolve question B, even though it targets the
    # very next ask() call on the same channel.
    assert disp.answer_question(spec.id, {"choice": "retry", "text": "try again"}) == (
        400, {"error": "unknown choice"})
    assert thread_b.is_alive(), "question B must still be waiting"

    assert disp.answer_question(spec.id, {"choice": "approve"}) == (202, {})
    thread_b.join(timeout=5)
    assert box_b["reply"] == Reply(choice="approve")


def test_answer_question_no_pending_question_is_409():
    # AC8: never a silent no-op 202 — a job that was never interactive, or
    # is interactive but not currently paused, both answer 409.
    disp = Dispatcher(token="x")
    never_interactive = JobSpec(
        mode="deliver", repo="a/b", title="T", description="D",
        budget_usd=None, id="ans-a",
    )
    disp._registry["ans-a"] = JobRecord(spec=never_interactive)
    assert disp.answer_question("ans-a", {"choice": "approve"}) == (
        409, {"error": "no pending question"})

    not_paused = _interactive_spec("ans-b")
    channel = dispatch_mod._TrackedChannel(timeout=5)
    disp._registry["ans-b"] = JobRecord(spec=not_paused, channel=channel)
    assert disp.answer_question("ans-b", {"choice": "approve"}) == (
        409, {"error": "no pending question"})

    assert disp.answer_question("ghost", {"choice": "approve"}) == (
        404, {"error": "unknown job"})


def test_get_question_pending_states_and_unknown_job():
    # AC6: false for a non-interactive job, an interactive job with no live
    # pause yet, and after a question has been answered; true with the live
    # Question's shape while ask() is blocked; 404 for an unknown job id.
    disp = Dispatcher(token="x")

    never_interactive = JobSpec(
        mode="deliver", repo="a/b", title="T", description="D",
        budget_usd=None, id="q-a",
    )
    disp._registry["q-a"] = JobRecord(spec=never_interactive)
    assert disp.get_question("q-a") == (200, {"pending": False})

    not_paused = _interactive_spec("q-b")
    channel = dispatch_mod._TrackedChannel(timeout=5)
    disp._registry["q-b"] = JobRecord(spec=not_paused, channel=channel)
    assert disp.get_question("q-b") == (200, {"pending": False})

    channel.current = _sample_question()
    status, payload = disp.get_question("q-b")
    assert status == 200
    assert payload == {
        "pending": True,
        "prompt": "Approve?",
        "context": "plan text",
        "choices": [
            {"key": "approve", "label": "go", "accepts_text": False},
            {"key": "abort", "label": "stop", "accepts_text": False},
        ],
        "default": "approve",
    }

    channel.current = None  # answered
    assert disp.get_question("q-b") == (200, {"pending": False})

    assert disp.get_question("ghost") == (404, {"error": "unknown job"})


def test_tracked_channel_timeout_takes_default_and_clears_current():
    # AC9: nobody answers within the bound -> the question's default Reply
    # is taken, and GET .../question reflects {"pending": false} again
    # afterward via the finally clear. No real time.sleep — the short
    # timeout is the QueueChannel's own bounded wait, same convention as
    # test_queue_channel_timeout_takes_default.
    disp = Dispatcher(token="x")
    channel = dispatch_mod._TrackedChannel(timeout=0.01)
    spec = _interactive_spec("timeout-1")
    disp._registry[spec.id] = JobRecord(spec=spec, channel=channel)

    reply = channel.ask(_sample_question())
    assert reply == Reply(choice="approve")
    assert channel.current is None
    assert disp.get_question(spec.id) == (200, {"pending": False})


def test_question_and_answer_http_routes_round_trip():
    with running(materialise=_mem_materialise) as server:
        disp = server.dispatcher
        channel = dispatch_mod._TrackedChannel(timeout=5)
        spec = _interactive_spec("http-qa")
        disp._registry["http-qa"] = JobRecord(spec=spec, channel=channel)
        disp._order.append("http-qa")

        assert _call(server, "/jobs/http-qa/question") == (200, {"pending": False})

        box = {}

        def ask():
            box["reply"] = channel.ask(_sample_question())

        thread = threading.Thread(target=ask)
        thread.start()
        _await_pending(channel)

        status, payload = _call(server, "/jobs/http-qa/question")
        assert status == 200
        assert payload["pending"] is True
        assert payload["default"] == "approve"

        assert _call(
            server, "/jobs/http-qa/answer", method="POST", body={"choice": "nope"}
        ) == (400, {"error": "unknown choice"})

        assert _call(
            server, "/jobs/http-qa/answer", method="POST",
            body={"choice": "approve", "text": "go ahead"},
        ) == (202, {})
        thread.join(timeout=5)
        assert box["reply"] == Reply(choice="approve", text="go ahead")

        assert _call(server, "/jobs/unknown/question") == (
            404, {"error": "unknown job"})
        assert _call(
            server, "/jobs/unknown/answer", method="POST", body={"choice": "x"}
        ) == (404, {"error": "unknown job"})


def test_question_and_answer_http_routes_require_auth():
    # AC11 [security]: the two new endpoints are not accidentally exempted
    # from the existing bearer-auth gate.
    with running(materialise=_mem_materialise) as server:
        assert _call(server, "/jobs/x/question", token=None) == (
            401, {"error": "unauthorized"})
        assert _call(
            server, "/jobs/x/answer", method="POST", token=None,
            body={"choice": "approve"},
        ) == (401, {"error": "unauthorized"})


def test_answer_http_route_rejects_malformed_body():
    # A malformed JSON body is rejected by _read_body's own 400 before
    # answer_question is ever reached (mirrors _create's malformed-body
    # handling for POST /jobs).
    with running(materialise=_mem_materialise) as server:
        status, payload = _call(
            server, "/jobs/x/answer", method="POST", body=b"{not json"
        )
        assert status == 400
        assert "error" in payload


def test_jobs_list_archived_query_param_reveals_archived_jobs():
    dash = InMemoryWorkspace()
    with running(runner=_assess_runner(), materialise=_mem_materialise,
                 dashboard_workspace=dash) as server:
        _, job = _call(server, "/jobs", method="POST",
                       body={"mode": "assess", "repo": "a/one"})
        server.dispatcher.wait(job["id"], 5)
        _call(server, f"/jobs/{job['id']}/archive", method="POST")

        status, payload = _call(server, "/jobs")
        assert status == 200
        assert payload["jobs"] == []

        status, payload = _call(server, "/jobs?archived=1")
        assert status == 200
        assert [j["id"] for j in payload["jobs"]] == [job["id"]]


def test_jobs_list_http_route_paginates_with_limit_and_offset():
    # U10(c): the GET /jobs route threads bounded ?limit=/?offset= into
    # recent(); jobs are inserted directly to page over >25 without 30 runs.
    with running(materialise=_mem_materialise) as server:
        for i in range(30):
            _insert_job(server.dispatcher, f"assess-{i:02d}", "assess", "queued", None)
        newest_first = [f"assess-{i:02d}" for i in range(29, -1, -1)]

        status, payload = _call(server, "/jobs?limit=5")
        assert status == 200
        assert [j["id"] for j in payload["jobs"]] == newest_first[:5]

        _, payload = _call(server, "/jobs?limit=5&offset=5")
        assert [j["id"] for j in payload["jobs"]] == newest_first[5:10]

        # absent params keep the default cap of 25; a non-numeric value falls
        # back to that same default rather than erroring.
        assert len(_call(server, "/jobs")[1]["jobs"]) == 25
        assert len(_call(server, "/jobs?limit=notanint")[1]["jobs"]) == 25


def test_worker_is_single_flight_and_ordered():
    order = []
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        order.append(spec.id)
        if len(order) == 1:
            first_in.set()
            release.wait(5)  # hold job 1 so job 2 must wait its turn
        return _clone_ws()

    disp = Dispatcher(token="x", runner=_assess_runner(), materialise=materialise)
    disp.start()
    try:
        id1, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/one"}))
        id2, _ = disp.submit(disp.build_spec({"mode": "assess", "repo": "a/two"}))
        assert first_in.wait(5)
        # job 1 is running (mid-materialise); job 2 has not started yet
        assert disp.get(id1).state == "running"
        assert disp.get(id2).state == "queued"
        release.set()
        assert disp.wait(id1, 5) and disp.wait(id2, 5)
        assert order == [id1, id2]
        assert disp.get(id2).state == "succeeded"
    finally:
        release.set()
        disp.stop()


def test_default_materialise_clones_and_returns_local_workspace(tmp_path, monkeypatch):
    calls = {}

    def fake_clone(ref, dest, *, runner, token=None, timeout=None):
        calls.update(slug=ref.slug, dest=dest, token=token)
        Path(dest).mkdir(parents=True, exist_ok=True)
        return dest

    monkeypatch.setattr(dispatch_mod, "clone_or_update", fake_clone)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    spec = JobSpec(mode="assess", repo="acme/mono", title="t", description="",
                   budget_usd=None, id="assess-1")
    ws = _default_materialise(spec, str(tmp_path / "clone"))
    assert calls["slug"] == "acme/mono"
    assert isinstance(ws, LocalWorkspace)


# --- the HTTP server ----------------------------------------------------------


@contextlib.contextmanager
def running(**kwargs):
    server = DispatchServer(TOKEN, port=0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _call(server, path, *, method="GET", token=TOKEN, body=None):
    url = server.url.rstrip("/") + path
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        exc.close()  # HTTPError carries the response socket
        return exc.code, payload


def test_health_needs_no_auth_and_reports_version():
    with running(materialise=_mem_materialise) as server:
        status, payload = _call(server, "/health", token=None)
    assert status == 200
    assert payload == {
        "status": "ok",
        "service": "dev-team-dispatch",
        "version": __version__,
    }


def test_url_names_host_and_port():
    with running(materialise=_mem_materialise) as server:
        assert server.url.startswith("http://127.0.0.1:")
        assert server.url.endswith("/")


def test_unauthorized_without_and_with_wrong_token():
    with running(materialise=_mem_materialise) as server:
        assert _call(server, "/jobs", token=None) == (401, {"error": "unauthorized"})
        assert _call(server, "/jobs", token="wrong") == (401, {"error": "unauthorized"})
        status, payload = _call(
            server, "/jobs", method="POST", token=None,
            body={"mode": "assess", "repo": "a/b"},
        )
        assert (status, payload) == (401, {"error": "unauthorized"})


def _raw_request(server, method, path, *, headers=None, body=None):
    """One raw request via http.client — urllib mangles odd header bytes and
    validates Content-Length, so it cannot exercise the malformed-header paths."""

    host, port = server.httpd.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        return res.status, res.read().decode()
    finally:
        conn.close()


def test_non_ascii_bearer_is_a_clean_401():
    # Headers decode latin-1, so a non-ASCII Authorization value is a valid str
    # that hmac.compare_digest refuses on str — comparing bytes turns that into
    # a clean 401 instead of an unhandled 500/connection reset (a pre-auth DoS).
    with running(materialise=_mem_materialise) as server:
        status, body = _raw_request(
            server, "GET", "/jobs",
            headers={"Authorization": "Bearer wrongÿ"},
        )
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}


def test_create_rejects_non_numeric_content_length():
    # A non-numeric Content-Length must not crash int() into a 500.
    with running(materialise=_mem_materialise) as server:
        status, body = _raw_request(
            server, "POST", "/jobs",
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Length": "not-a-number"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "malformed Content-Length"}


def test_create_rejects_negative_content_length():
    # A negative length parses fine but would drive rfile.read(-1) to slurp the
    # whole stream; it is rejected up front instead.
    with running(materialise=_mem_materialise) as server:
        status, body = _raw_request(
            server, "POST", "/jobs",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": "-1"},
        )
    assert status == 400
    assert json.loads(body) == {"error": "malformed Content-Length"}


def test_create_rejects_oversized_body():
    # An oversized (or lying) Content-Length is rejected without buffering the
    # body — no unbounded read.
    with running(materialise=_mem_materialise) as server:
        status, body = _raw_request(
            server, "POST", "/jobs",
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Length": str((1 << 20) + 1)},
        )
    assert status == 413
    assert json.loads(body) == {"error": "request body too large"}


def test_submit_assess_flows_to_a_result():
    with running(runner=_assess_runner(), materialise=_mem_materialise) as server:
        status, payload = _call(
            server, "/jobs", method="POST", body={"mode": "assess", "repo": "acme/mono"}
        )
        assert status == 202
        assert payload["state"] == "queued"
        assert payload["position"] == 0
        job_id = payload["id"]
        assert server.dispatcher.wait(job_id, 5)

        status, job = _call(server, f"/jobs/{job_id}")
        assert status == 200
        assert job["state"] == "succeeded"
        assert job["mode"] == "assess"
        assert job["progress"], "a finished assess should have journalled progress"
        assert set(job["progress"][0]) == {"role", "stage", "message", "ts"}

        status, result = _call(server, f"/jobs/{job_id}/result")
        assert status == 200
        assert result["kind"] == "assess"
        assert result["success"] is True
        assert result["classification"] == "dependency-surgery"
        assert "report_markdown" in result


def test_submit_deliver_flows_to_a_result():
    with running(runner=_deliver_runner(), materialise=_mem_materialise) as server:
        status, payload = _call(
            server, "/jobs", method="POST",
            body={"mode": "deliver", "repo": "acme/mono", "title": "F", "description": "d"},
        )
        assert status == 202
        job_id = payload["id"]
        assert server.dispatcher.wait(job_id, 5)
        status, result = _call(server, f"/jobs/{job_id}/result")
        assert status == 200
        assert result["kind"] == "deliver"
        assert result["success"] is True


def test_submit_validation_errors_are_400():
    with running(materialise=_mem_materialise) as server:
        bad_bodies = [
            {"mode": "nope", "repo": "a/b"},
            {"mode": "assess", "repo": ""},
            {"mode": "assess", "repo": "a/b", "budget_usd": -1},
            {"mode": "deliver", "repo": "a/b", "description": "d"},  # no title
        ]
        for body in bad_bodies:
            status, payload = _call(server, "/jobs", method="POST", body=body)
            assert status == 400
            assert "error" in payload
        # malformed JSON and a non-object body
        assert _call(server, "/jobs", method="POST", body=b"{not json")[0] == 400
        assert _call(server, "/jobs", method="POST", body=b"123")[0] == 400
        # an empty body (no Content-Length) -> treated as {} -> missing mode
        assert _call(server, "/jobs", method="POST", body=None)[0] == 400


def test_queue_full_returns_503():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    with running(runner=_assess_runner(), materialise=materialise, queue_cap=1) as server:
        # job 1 is picked up and blocks in materialise (running, not queued)
        _call(server, "/jobs", method="POST", body={"mode": "assess", "repo": "a/one"})
        assert first_in.wait(5)
        # job 2 fills the single queue slot
        assert _call(server, "/jobs", method="POST",
                     body={"mode": "assess", "repo": "a/two"})[0] == 202
        # job 3 overflows -> 503
        assert _call(server, "/jobs", method="POST",
                     body={"mode": "assess", "repo": "a/three"}) == (
            503, {"error": "queue full"})
        release.set()


def test_list_jobs_is_newest_first():
    with running(runner=_assess_runner(), materialise=_mem_materialise) as server:
        _, one = _call(server, "/jobs", method="POST", body={"mode": "assess", "repo": "a/one"})
        server.dispatcher.wait(one["id"], 5)
        _, two = _call(server, "/jobs", method="POST", body={"mode": "assess", "repo": "a/two"})
        server.dispatcher.wait(two["id"], 5)
        status, payload = _call(server, "/jobs")
        assert status == 200
        ids = [j["id"] for j in payload["jobs"]]
        assert ids == [two["id"], one["id"]]
        assert set(payload["jobs"][0]) == {"id", "mode", "repo", "state", "started", "ended"}


def test_status_and_result_across_queued_running_and_404():
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        first_in.set()
        release.wait(5)
        return InMemoryWorkspace()

    with running(runner=_assess_runner(), materialise=materialise) as server:
        _, one = _call(server, "/jobs", method="POST", body={"mode": "assess", "repo": "a/one"})
        assert first_in.wait(5)
        _, two = _call(server, "/jobs", method="POST", body={"mode": "assess", "repo": "a/two"})

        _, running_job = _call(server, f"/jobs/{one['id']}")
        assert running_job["state"] == "running"
        assert running_job["progress"] == []  # workspace not assigned yet

        _, queued_job = _call(server, f"/jobs/{two['id']}")
        assert queued_job["state"] == "queued"
        assert queued_job["started"] is None

        assert _call(server, f"/jobs/{one['id']}/result") == (
            409, {"error": "not finished", "state": "running"})
        assert _call(server, f"/jobs/{two['id']}/result") == (
            409, {"error": "not finished", "state": "queued"})

        assert _call(server, "/jobs/ghost") == (404, {"error": "unknown job"})
        assert _call(server, "/jobs/ghost/result") == (404, {"error": "unknown job"})
        release.set()
        server.dispatcher.wait(one["id"], 5)
        server.dispatcher.wait(two["id"], 5)


def test_failed_job_status_and_result():
    def boom(spec, dest):
        raise RuntimeError("materialise failed")

    with running(materialise=boom) as server:
        _, payload = _call(server, "/jobs", method="POST",
                           body={"mode": "assess", "repo": "a/b"})
        job_id = payload["id"]
        assert server.dispatcher.wait(job_id, 5)
        _, job = _call(server, f"/jobs/{job_id}")
        assert job["state"] == "failed"
        assert "materialise failed" in job["error"]
        status, result = _call(server, f"/jobs/{job_id}/result")
        assert status == 200
        # materialise raised before any budget existed, so the served cost is
        # a genuine 0.0 (no partial spend to report), not a hard-coded 0.
        assert result == {
            "kind": "assess",
            "success": False,
            "error": job["error"],
            "cost_usd": 0.0,
        }


def test_post_jobs_with_backlog_true_flows_to_stories():
    dash = InMemoryWorkspace()
    with running(
        runner=_assess_runner(), materialise=_mem_materialise, dashboard_workspace=dash
    ) as server:
        status, payload = _call(
            server, "/jobs", method="POST",
            body={"mode": "assess", "repo": "acme/mono", "backlog": True},
        )
        assert status == 202
        job_id = payload["id"]
        assert server.dispatcher.wait(job_id, 5)
        # the run itself merged the stories into the dashboard workspace
        assert dash.exists(".dev_team/backlog.json")
        stories = json.loads(dash.read_text(".dev_team/backlog.json"))["stories"]
        assert stories
        # calling the later endpoint again is idempotent (dedup by title)
        status, result = _call(server, f"/jobs/{job_id}/backlog", method="POST")
        assert status == 200
        assert result == {
            "job_id": job_id,
            "stories_added": 0,
            "stories_total": len(stories),
        }
        # a non-bool backlog flag is rejected up front
        assert _call(
            server, "/jobs", method="POST",
            body={"mode": "assess", "repo": "acme/mono", "backlog": "yes"},
        )[0] == 400


def test_post_jobs_id_backlog_generates_after_the_fact():
    dash = InMemoryWorkspace()
    with running(
        runner=_assess_runner(), materialise=_mem_materialise, dashboard_workspace=dash
    ) as server:
        _, payload = _call(
            server, "/jobs", method="POST", body={"mode": "assess", "repo": "acme/mono"}
        )
        job_id = payload["id"]
        assert server.dispatcher.wait(job_id, 5)
        assert not dash.exists(".dev_team/backlog.json")  # nothing yet
        status, result = _call(server, f"/jobs/{job_id}/backlog", method="POST")
        assert status == 200
        assert set(result) == {"job_id", "stories_added", "stories_total"}
        assert result["job_id"] == job_id
        assert result["stories_added"] > 0
        assert result["stories_total"] == result["stories_added"]
        assert dash.exists(".dev_team/backlog.json")


def test_post_jobs_id_backlog_auth_and_error_contract():
    dash = InMemoryWorkspace()
    with running(materialise=_mem_materialise, dashboard_workspace=dash) as server:
        assert _call(server, "/jobs/x/backlog", method="POST", token=None) == (
            401, {"error": "unauthorized"})
        assert _call(server, "/jobs/x/backlog", method="POST") == (
            404, {"error": "no assessment for that job"})
    with running(materialise=_mem_materialise) as server:  # no dashboard workspace
        assert _call(server, "/jobs/x/backlog", method="POST") == (
            409, {"error": "backlog generation needs a dashboard workspace"})


def test_unknown_routes_are_404():
    with running(materialise=_mem_materialise) as server:
        assert _call(server, "/nope")[0] == 404
        assert _call(server, "/jobs/abc/extra")[0] == 404  # 2 segments after jobs, not result
        assert _call(server, "/nope", method="POST", body={})[0] == 404
        # 2 segments after jobs but not "backlog" -> still 404
        assert _call(server, "/jobs/abc/extra", method="POST", body={})[0] == 404


# --- the backlog mutation API (the Kanban board's write path) ------------------


def _board_dash():
    """A dashboard workspace holding a small backlog (S1, S2 under E1)."""

    dash = InMemoryWorkspace()
    store = BacklogStore(dash)
    backlog = store.load()
    epic = backlog.add_epic("Remediation — acme/mono", "")
    backlog.add_story("Pin build chain", "CI", estimate=3, epic_id=epic.id)
    backlog.add_story("Upgrade ORM", "big bang", estimate=8, epic_id=epic.id)
    store.save(backlog)
    return dash


def _board_dispatcher():
    dash = _board_dash()
    return Dispatcher(token="x", dashboard_workspace=dash, clock=lambda: 111.0), dash


def test_board_returns_the_full_backlog_or_409():
    disp, _ = _board_dispatcher()
    status, payload = disp.board()
    assert status == 200
    assert [s["id"] for s in payload["stories"]] == ["S1", "S2"]
    assert [e["id"] for e in payload["epics"]] == ["E1"]
    assert Dispatcher(token="x").board() == (
        409, {"error": "backlog needs a dashboard workspace"})


def test_set_story_status_mutates_stamps_and_persists():
    disp, dash = _board_dispatcher()
    status, payload = disp.set_story_status("S1", "in_progress")
    assert status == 200
    assert payload["id"] == "S1"
    assert payload["status"] == "in_progress"
    assert payload["updated_at"] == 111.0
    stored = BacklogStore(dash).load()
    assert stored.stories[0].status.value == "in_progress"
    assert stored.stories[0].updated_at == 111.0


def test_set_story_status_error_contract():
    disp, dash = _board_dispatcher()
    status, payload = disp.set_story_status("S1", "shipped")
    assert status == 400
    assert "status must be one of" in payload["error"]
    assert disp.set_story_status("S99", "done") == (404, {"error": "unknown story"})
    assert Dispatcher(token="x").set_story_status("S1", "done") == (
        409, {"error": "backlog needs a dashboard workspace"})
    # rejected mutations are never persisted
    assert BacklogStore(dash).load().stories[0].status.value == "todo"


def test_decline_story_sets_the_declined_status():
    disp, dash = _board_dispatcher()
    status, payload = disp.decline_story("S2")
    assert status == 200
    assert payload["status"] == "declined"
    assert BacklogStore(dash).load().stories[1].status.value == "declined"
    assert disp.decline_story("S99") == (404, {"error": "unknown story"})


def test_edit_story_applies_only_provided_keys():
    disp, dash = _board_dispatcher()
    status, payload = disp.edit_story(
        "S1", {"title": "Pin the build chain", "description": "CI + local"}
    )
    assert status == 200
    assert payload["title"] == "Pin the build chain"
    assert payload["description"] == "CI + local"
    assert payload["estimate"] == 3  # untouched
    status, payload = disp.edit_story("S1", {"estimate": 5})
    assert status == 200
    assert payload["estimate"] == 5
    assert payload["title"] == "Pin the build chain"  # untouched
    stored = BacklogStore(dash).load()
    assert stored.stories[0].title == "Pin the build chain"
    assert stored.stories[0].estimate == 5


def test_edit_story_error_contract():
    disp, dash = _board_dispatcher()
    for fields in (
        {"title": ""},
        {"title": 7},
        {"description": 7},
        {"estimate": 0},
        {"estimate": True},
        {"estimate": "3"},
    ):
        status, payload = disp.edit_story("S1", fields)
        assert status == 400
        assert "error" in payload
    assert disp.edit_story("S99", {"title": "x"}) == (404, {"error": "unknown story"})
    assert Dispatcher(token="x").edit_story("S1", {"title": "x"}) == (
        409, {"error": "backlog needs a dashboard workspace"})
    assert BacklogStore(dash).load().stories[0].title == "Pin build chain"


def test_add_story_card_defaults_and_full_fields():
    disp, dash = _board_dispatcher()
    status, payload = disp.add_story_card({"title": "Write the runbook"})
    assert status == 201
    assert payload["id"] == "S3"
    assert (payload["estimate"], payload["status"]) == (1, "todo")
    assert payload["updated_at"] == 111.0
    status, payload = disp.add_story_card(
        {"title": "Wire alerts", "description": "pager", "estimate": 2,
         "epic_id": "E1", "status": "in_progress"}
    )
    assert status == 201
    assert payload["id"] == "S4"
    assert payload["epic_id"] == "E1"
    assert payload["status"] == "in_progress"
    assert len(BacklogStore(dash).load().stories) == 4


def test_add_story_card_error_contract():
    disp, _ = _board_dispatcher()
    for fields in (
        {},
        {"title": "  "},
        {"title": 7},
        {"title": "ok", "description": 7},
        {"title": "ok", "estimate": 0},
        {"title": "ok", "estimate": True},
        {"title": "ok", "epic_id": 7},
        {"title": "ok", "status": "shipped"},
    ):
        status, payload = disp.add_story_card(fields)
        assert status == 400
        assert "error" in payload
    assert Dispatcher(token="x").add_story_card({"title": "x"}) == (
        409, {"error": "backlog needs a dashboard workspace"})


def test_delete_story_strips_inbound_edges_and_never_reissues_the_id():
    disp, dash = _board_dispatcher()
    assert disp.set_story_deps("S2", ["S1"])[0] == 200
    status, payload = disp.delete_story("S1")
    assert status == 200
    assert payload["id"] == "S1"
    stored = BacklogStore(dash).load()
    assert [s.id for s in stored.stories] == ["S2"]
    assert stored.stories[0].depends_on == []  # the dangling edge is stripped
    # the freed id is never reused: minting scans past the highest suffix
    status, payload = disp.add_story_card({"title": "Newcomer"})
    assert (status, payload["id"]) == (201, "S3")
    assert disp.delete_story("S1") == (404, {"error": "unknown story"})
    assert Dispatcher(token="x").delete_story("S1") == (
        409, {"error": "backlog needs a dashboard workspace"})


def test_set_story_deps_validates_and_persists():
    disp, dash = _board_dispatcher()
    status, payload = disp.set_story_deps("S2", ["S1"])
    assert status == 200
    assert payload["depends_on"] == ["S1"]
    assert BacklogStore(dash).load().stories[1].depends_on == ["S1"]
    # clearing the edges is a normal write
    status, payload = disp.set_story_deps("S2", [])
    assert status == 200
    assert "depends_on" not in payload  # empty list is omitted on the wire


def test_set_story_deps_error_contract():
    disp, dash = _board_dispatcher()
    status, payload = disp.set_story_deps("S2", "S1")
    assert status == 400
    assert payload["error"] == "depends_on must be a list of story ids"
    assert disp.set_story_deps("S2", [7])[0] == 400
    status, payload = disp.set_story_deps("S2", ["S99"])
    assert status == 400
    assert "unknown story 'S99'" in payload["error"]
    status, payload = disp.set_story_deps("S2", ["S2"])
    assert status == 400
    assert "depends on itself" in payload["error"]
    assert disp.set_story_deps("S99", ["S1"]) == (404, {"error": "unknown story"})
    assert Dispatcher(token="x").set_story_deps("S1", []) == (
        409, {"error": "backlog needs a dashboard workspace"})
    assert BacklogStore(dash).load().stories[1].depends_on == []


def test_set_story_deps_rejects_a_cycle_without_persisting_it():
    disp, dash = _board_dispatcher()
    assert disp.set_story_deps("S2", ["S1"])[0] == 200
    status, payload = disp.set_story_deps("S1", ["S2"])
    assert status == 400
    assert "Dependency cycle" in payload["error"]
    # the rejected edge was never saved; the earlier one survives
    stored = BacklogStore(dash).load()
    assert stored.stories[0].depends_on == []
    assert stored.stories[1].depends_on == ["S1"]


def test_backlog_write_lock_prevents_lost_updates():
    """Two handler threads + a worker-style merge on ONE dispatcher.

    Every core (and _merge_backlog) is a read-modify-write of the same
    backlog.json; the injected clock sleeps INSIDE the critical section, so
    without the shared lock these writers would overwrite each other and
    stories would vanish. With it, the final count is exactly the sum.
    """

    dash = InMemoryWorkspace()

    def slow_clock():
        time.sleep(0.002)  # widen the load→save window the lock must cover
        return time.time()

    disp = Dispatcher(token="x", dashboard_workspace=dash, clock=slow_clock)
    per_thread = 10

    def add_cards(tag):
        for index in range(per_thread):
            assert disp.add_story_card({"title": f"{tag}-{index}"})[0] == 201

    def merge_backlogs():
        for index in range(per_thread):
            disp._merge_backlog(
                _assessment_payload(), repo=f"acme/r{index}", source_job=f"job-{index}"
            )

    threads = [
        threading.Thread(target=add_cards, args=("a",)),
        threading.Thread(target=add_cards, args=("b",)),
        threading.Thread(target=merge_backlogs),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    status, board = disp.board()
    assert status == 200
    # 2 threads x 10 cards + 10 merges x 1 plan story each = 30, none lost
    assert len(board["stories"]) == 3 * per_thread
    ids = [story["id"] for story in board["stories"]]
    assert len(set(ids)) == len(ids)  # minting under the lock never collides


def test_backlog_http_routes_end_to_end():
    dash = _board_dash()
    with running(materialise=_mem_materialise, dashboard_workspace=dash) as server:
        status, board = _call(server, "/backlog")
        assert status == 200
        assert [s["id"] for s in board["stories"]] == ["S1", "S2"]

        status, story = _call(
            server, "/backlog/story", method="POST",
            body={"title": "Write the runbook", "estimate": 2},
        )
        assert (status, story["id"]) == (201, "S3")

        status, story = _call(
            server, "/backlog/story/S1/status", method="POST",
            body={"status": "done"},
        )
        assert (status, story["status"]) == (200, "done")

        status, story = _call(server, "/backlog/story/S2/decline", method="POST")
        assert (status, story["status"]) == (200, "declined")

        status, story = _call(
            server, "/backlog/story/S3/deps", method="POST",
            body={"depends_on": ["S1"]},
        )
        assert (status, story["depends_on"]) == (200, ["S1"])
        assert _call(
            server, "/backlog/story/S3/deps", method="POST",
            body={"depends_on": ["S3"]},
        )[0] == 400

        status, story = _call(
            server, "/backlog/story/S3", method="PATCH",
            body={"title": "Write THE runbook"},
        )
        assert (status, story["title"]) == (200, "Write THE runbook")

        status, story = _call(server, "/backlog/story/S1", method="DELETE")
        assert (status, story["id"]) == (200, "S1")
        status, board = _call(server, "/backlog")
        assert [s["id"] for s in board["stories"]] == ["S2", "S3"]
        # S3's edge on the deleted S1 was stripped (and, empty, is omitted)
        assert "depends_on" not in board["stories"][1]


def test_backlog_routes_require_auth():
    dash = _board_dash()
    with running(materialise=_mem_materialise, dashboard_workspace=dash) as server:
        unauthorized = (401, {"error": "unauthorized"})
        assert _call(server, "/backlog", token=None) == unauthorized
        assert _call(server, "/backlog/story", method="POST", token=None,
                     body={"title": "x"}) == unauthorized
        assert _call(server, "/backlog/story/S1/status", method="POST",
                     token="wrong", body={"status": "done"}) == unauthorized
        assert _call(server, "/backlog/story/S1", method="PATCH", token=None,
                     body={"title": "x"}) == unauthorized
        assert _call(server, "/backlog/story/S1", method="DELETE",
                     token=None) == unauthorized
        # nothing above touched the backlog
        status, board = _call(server, "/backlog")
        assert [s["title"] for s in board["stories"]] == [
            "Pin build chain", "Upgrade ORM"]


def test_backlog_routes_unknown_paths_and_bad_bodies():
    dash = _board_dash()
    with running(materialise=_mem_materialise, dashboard_workspace=dash) as server:
        # unknown shapes under /backlog are 404 on every method
        assert _call(server, "/backlog", method="POST", body={})[0] == 404
        assert _call(server, "/backlog/story/S1", method="POST", body={})[0] == 404
        assert _call(server, "/backlog/story/S1/promote", method="POST",
                     body={})[0] == 404
        assert _call(server, "/backlog/nope/S1/status", method="POST",
                     body={})[0] == 404
        assert _call(server, "/nope", method="PATCH", body={})[0] == 404
        assert _call(server, "/backlog/story", method="PATCH", body={})[0] == 404
        assert _call(server, "/nope", method="DELETE")[0] == 404
        assert _call(server, "/backlog/story", method="DELETE")[0] == 404
        # malformed bodies get the shared 400, per route family
        assert _call(server, "/backlog/story", method="POST",
                     body=b"{not json")[0] == 400
        assert _call(server, "/backlog/story/S1/status", method="POST",
                     body=b"[1]")[0] == 400
        assert _call(server, "/backlog/story/S1/deps", method="POST",
                     body=b"{not json")[0] == 400
        assert _call(server, "/backlog/story/S1", method="PATCH",
                     body=b"{not json")[0] == 400
        # a body-less status change is a plain validation 400, not a crash
        assert _call(server, "/backlog/story/S1/status", method="POST")[0] == 400


# --- finding re-verification (mode "verify" + the read routes) ------------------


def _seeded_dash(source="assess-old", *, meta=True):
    """A dashboard workspace as a finished assess job leaves it (post-restart).

    Only disk state — assessment.json + meta.json under audit/<source>/ —
    exactly what a FRESH dispatcher must be able to verify from.
    """

    dash = InMemoryWorkspace()
    dash.write_text(
        f"audit/{source}/assessment.json", json.dumps(_assessment_payload())
    )
    if meta:
        dash.write_text(
            f"audit/{source}/meta.json",
            json.dumps({"repo": "acme/mono", "mode": "assess", "id": source}),
        )
    return dash


def _verifier_runner(payload=None):
    from dev_team.testing import json_response

    payload = payload or {
        "verdict": "confirmed",
        "rationale": "checked the build files",
        "citations": [{"path": "global.json", "note": "pin exists"}],
    }
    return ScriptedRunner(
        by_system_prompt={"application security engineer": json_response(payload)}
    )


def test_build_spec_verify_requires_source_job_and_finding_id():
    disp = Dispatcher(token="x", dashboard_workspace=_seeded_dash())
    for body in (
        {"mode": "verify"},
        {"mode": "verify", "source_job": "   "},
        {"mode": "verify", "source_job": 7, "finding_id": "x"},
        {"mode": "verify", "source_job": "assess-old"},
        {"mode": "verify", "source_job": "assess-old", "finding_id": ""},
        {"mode": "verify", "source_job": "assess-old", "finding_id": 3},
    ):
        with pytest.raises(ValidationError):
            disp.build_spec(body)


def test_build_spec_verify_without_dashboard_workspace_is_409():
    with pytest.raises(SubmitRejected) as excinfo:
        Dispatcher(token="x").build_spec(
            {"mode": "verify", "source_job": "a", "finding_id": "b"}
        )
    assert excinfo.value.status == 409
    assert "dashboard workspace" in str(excinfo.value)


def test_build_spec_verify_missing_assessment_meta_or_traversal_is_404():
    disp = Dispatcher(token="x", dashboard_workspace=_seeded_dash(meta=False))
    for source in ("assess-old", "ghost", "../escape"):
        with pytest.raises(SubmitRejected) as excinfo:
            disp.build_spec(
                {"mode": "verify", "source_job": source, "finding_id": "x"}
            )
        assert excinfo.value.status == 404
        assert str(excinfo.value) == "no assessment for that job"


def test_build_spec_verify_unresolvable_finding_is_404():
    disp = Dispatcher(token="x", dashboard_workspace=_seeded_dash())
    with pytest.raises(SubmitRejected) as excinfo:
        disp.build_spec(
            {"mode": "verify", "source_job": "assess-old",
             "finding_id": "no such claim anywhere"}
        )
    assert excinfo.value.status == 404
    assert str(excinfo.value) == "finding not found"


def test_build_spec_verify_corrupt_assessment_is_404():
    dash = InMemoryWorkspace()
    dash.write_text("audit/assess-bad/assessment.json", "{not json")
    dash.write_text(
        "audit/assess-bad/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-bad"}),
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    with pytest.raises(SubmitRejected) as excinfo:
        disp.build_spec(
            {"mode": "verify", "source_job": "assess-bad", "finding_id": "x"}
        )
    assert excinfo.value.status == 404
    assert str(excinfo.value) == "no assessment for that job"


def test_build_spec_verify_corrupt_meta_is_404():
    # assessment.json resolves the finding, but a corrupt meta.json (which
    # names the repo to re-clone) is a broken mirror -> 404, not a 500.
    dash = InMemoryWorkspace()
    dash.write_text(
        "audit/assess-cm/assessment.json", json.dumps(_assessment_payload())
    )
    dash.write_text("audit/assess-cm/meta.json", "{not json")
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    with pytest.raises(SubmitRejected) as excinfo:
        disp.build_spec(
            {"mode": "verify", "source_job": "assess-cm",
             "finding_id": "recommendation.plan[0]"}
        )
    assert excinfo.value.status == 404
    assert str(excinfo.value) == "no assessment for that job"


def test_build_spec_verify_resolves_repo_and_finding_from_disk():
    disp = Dispatcher(token="x", dashboard_workspace=_seeded_dash())
    # by claim substring (case-insensitive) — and by exact id below
    spec = disp.build_spec(
        {"mode": "verify", "source_job": " assess-old ",
         "finding_id": "pin BUILD", "budget_usd": 5}
    )
    assert spec.mode == "verify"
    assert spec.repo == "acme/mono"          # from audit/<source>/meta.json
    assert spec.source_job == "assess-old"
    assert spec.finding_id == "recommendation.plan[0]"
    assert spec.finding["claim"] == "Pin build chain"
    assert spec.budget_usd == 5
    assert spec.title == "verify recommendation.plan[0]"
    exact = disp.build_spec(
        {"mode": "verify", "source_job": "assess-old",
         "finding_id": "recommendation.plan[0]"}
    )
    assert exact.finding_id == "recommendation.plan[0]"
    assert exact.budget_usd is None


def test_run_job_assess_mirrors_meta_json():
    dash = InMemoryWorkspace()
    disp = Dispatcher(
        token="x",
        runner=_assess_runner(),
        materialise=_mem_materialise,
        dashboard_workspace=dash,
    )
    spec = disp.build_spec({"mode": "assess", "repo": "acme/mono"})
    spec.id = "assess-meta"
    asyncio.run(disp.run_job(JobRecord(spec=spec)))
    meta = json.loads(dash.read_text("audit/assess-meta/meta.json"))
    assert meta == {"repo": "acme/mono", "mode": "assess", "id": "assess-meta"}


def test_mirror_meta_and_verification_are_noops_without_dashboard():
    disp = Dispatcher(token="x")
    spec = JobSpec(mode="assess", repo="a/b", title="t", description="",
                   budget_usd=None, id="x")
    disp._mirror_meta(spec)                                  # touches nothing
    disp._mirror_verification("x", {"verdict": "confirmed"})  # ditto


def test_mirror_verification_appends_and_reader_is_chronological():
    dash = _seeded_dash()
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    disp._mirror_verification("assess-old", {"finding_id": "a", "verdict": "confirmed"})
    disp._mirror_verification("assess-old", {"finding_id": "b", "verdict": "refuted"})
    status, payload = disp.verifications("assess-old")
    assert status == 200
    assert [e["finding_id"] for e in payload["verifications"]] == ["a", "b"]


def test_verifications_reader_tolerates_blank_lines():
    dash = _seeded_dash()
    dash.write_text(
        "audit/assess-old/verifications.jsonl", '{"finding_id": "a"}\n\n'
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, payload = disp.verifications("assess-old")
    assert status == 200
    assert payload["verifications"] == [{"finding_id": "a"}]


def test_findings_and_verifications_are_disk_keyed_with_error_contract():
    disp = Dispatcher(token="x", dashboard_workspace=_seeded_dash())
    status, payload = disp.list_job_findings("assess-old")
    assert status == 200
    assert payload["job_id"] == "assess-old"
    ids = [f["id"] for f in payload["findings"]]
    assert "recommendation.plan[0]" in ids
    assert set(payload["findings"][0]) == {
        "id", "phase", "role", "claim", "evidence", "hash", "citation_broken",
    }
    # unknown job / traversal-shaped ids fail closed as 404
    for job_id in ("ghost", "../escape"):
        assert disp.list_job_findings(job_id) == (
            404, {"error": "no assessment for that job"})
        assert disp.verifications(job_id) == (
            404, {"error": "no assessment for that job"})
    # no dashboard workspace at all -> 409, like make_backlog
    bare = Dispatcher(token="x")
    assert bare.list_job_findings("any") == (
        409, {"error": "findings need a dashboard workspace"})
    assert bare.verifications("any") == (
        409, {"error": "verifications need a dashboard workspace"})


def test_list_job_findings_surfaces_citation_broken():
    """GET /jobs/{id}/findings joins broken_citations onto matching findings."""

    payload = {
        "classification": "dependency-surgery",
        "phases": {
            "risk": {
                "role": "security-engineer",
                "ok": True,
                "error": None,
                "data": {
                    "secrets": [
                        {"claim": "connection string committed",
                         "evidence": "does/not/exist.py"},
                        {"claim": "API key logged", "evidence": "src/real/file.py"},
                    ],
                },
            }
        },
        "broken_citations": {"risk": ["does/not/exist.py"]},
        "dead_code": {"findings": []},
        "dependency_scan": {"vulnerabilities": []},
    }
    dash = InMemoryWorkspace()
    dash.write_text("audit/assess-cb/assessment.json", json.dumps(payload))
    dash.write_text(
        "audit/assess-cb/meta.json",
        json.dumps({"repo": "acme/mono", "mode": "assess", "id": "assess-cb"}),
    )
    disp = Dispatcher(token="x", dashboard_workspace=dash)
    status, resp = disp.list_job_findings("assess-cb")
    assert status == 200
    by_id = {f["id"]: f for f in resp["findings"]}
    assert by_id["risk.secrets[0]"]["citation_broken"] is True
    assert by_id["risk.secrets[1]"]["citation_broken"] is False


def test_verify_job_end_to_end_on_a_fresh_dispatcher():
    """Simulated restart: only disk state, then submit → run → result."""

    dash = _seeded_dash()
    cloned = []

    def materialise(spec, dest):
        cloned.append(spec.repo)
        return InMemoryWorkspace()

    verifier = _verifier_runner()
    with running(
        runner=verifier, materialise=materialise, dashboard_workspace=dash
    ) as server:
        # findings are enumerable before any verify runs
        status, payload = _call(server, "/jobs/assess-old/findings")
        assert status == 200
        assert any(
            f["id"] == "recommendation.plan[0]" for f in payload["findings"]
        )
        status, payload = _call(
            server, "/jobs", method="POST",
            body={"mode": "verify", "source_job": "assess-old",
                  "finding_id": "recommendation.plan[0]", "budget_usd": 5},
        )
        assert status == 202
        assert payload["state"] == "queued"
        job_id = payload["id"]
        assert job_id.startswith("verify-")
        assert server.dispatcher.wait(job_id, 5)
        assert cloned == ["acme/mono"]  # re-cloned the SOURCE job's repo

        status, result = _call(server, f"/jobs/{job_id}/result")
        assert status == 200
        assert result == {
            "kind": "verify",
            "source_job": "assess-old",
            "finding_id": "recommendation.plan[0]",
            "verdict": "confirmed",
            "rationale": "checked the build files",
            "citations": [{"path": "global.json", "note": "pin exists"}],
            "cost_usd": 0.0,
        }
        # the verifier agent got read-only tools only
        (call,) = verifier.calls
        assert tuple(call["allowed_tools"]) == ("Read", "Grep", "Glob")

        status, verifs = _call(server, "/jobs/assess-old/verifications")
        assert status == 200
        assert verifs["job_id"] == "assess-old"
        (entry,) = verifs["verifications"]
        assert entry["finding_id"] == "recommendation.plan[0]"
        assert entry["verdict"] == "confirmed"
        assert entry["citations"] == [{"path": "global.json", "note": "pin exists"}]
        assert entry["cost_usd"] == 0.0
        assert "ts" in entry


def test_verify_job_agent_failure_becomes_a_failed_job():
    dash = _seeded_dash()
    verifier = ScriptedRunner(
        by_system_prompt={"application security engineer": "garbage, not json"}
    )
    with running(
        runner=verifier, materialise=_mem_materialise, dashboard_workspace=dash
    ) as server:
        _, payload = _call(
            server, "/jobs", method="POST",
            body={"mode": "verify", "source_job": "assess-old",
                  "finding_id": "Pin build chain"},
        )
        job_id = payload["id"]
        assert server.dispatcher.wait(job_id, 5)
        status, result = _call(server, f"/jobs/{job_id}/result")
        assert status == 200
        assert result["kind"] == "verify"
        assert result["success"] is False
        assert "unusable response" in result["error"]
        assert result["cost_usd"] == 0
        # a failed re-check never writes a verdict into the history
        _, verifs = _call(server, "/jobs/assess-old/verifications")
        assert verifs["verifications"] == []


def test_submit_verify_http_error_contract():
    with running(
        materialise=_mem_materialise, dashboard_workspace=_seeded_dash()
    ) as server:
        assert _call(
            server, "/jobs", method="POST",
            body={"mode": "verify", "source_job": "assess-old"},
        )[0] == 400
        assert _call(
            server, "/jobs", method="POST",
            body={"mode": "verify", "source_job": "ghost", "finding_id": "x"},
        ) == (404, {"error": "no assessment for that job"})
        assert _call(
            server, "/jobs", method="POST",
            body={"mode": "verify", "source_job": "assess-old",
                  "finding_id": "never matches anything"},
        ) == (404, {"error": "finding not found"})
    with running(materialise=_mem_materialise) as server:  # no dashboard ws
        assert _call(
            server, "/jobs", method="POST",
            body={"mode": "verify", "source_job": "a", "finding_id": "b"},
        ) == (409, {"error": "verify needs a dashboard workspace"})


def test_findings_and_verifications_routes_require_auth():
    with running(
        materialise=_mem_materialise, dashboard_workspace=_seeded_dash()
    ) as server:
        assert _call(server, "/jobs/assess-old/findings", token=None) == (
            401, {"error": "unauthorized"})
        assert _call(server, "/jobs/assess-old/verifications", token=None) == (
            401, {"error": "unauthorized"})
        # authorised: an assessed job with no verifications yet answers empty
        assert _call(server, "/jobs/assess-old/verifications") == (
            200, {"job_id": "assess-old", "verifications": []})


# --- access log ----------------------------------------------------------


def _access_records(root, *, expect, timeout=5.0, limit=300):
    """Access-log records, once at least ``expect`` of them are present.

    The dispatch handler appends each line in its ``finally`` — just after the
    response has returned to the client, and not synchronized with the client
    receiving it — so a test that reads the log the instant a call returns can
    race the handler's write (most visibly when it asserts an exact count).
    Poll until the expected number lands rather than assuming the append is
    synchronous with the response; returns as soon as they are there.
    """

    deadline = time.monotonic() + timeout
    while True:
        records = read_access_log(str(root), limit=limit)
        if len(records) >= expect or time.monotonic() >= deadline:
            return records
        time.sleep(0.01)


def test_access_log_records_unauthenticated_health_get(tmp_path):
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        _call(server, "/health", token=None)
    records = _access_records(tmp_path, expect=1)
    assert records[-1] == {**records[-1], "method": "GET", "path": "/health", "status": 200}


def test_access_log_records_authorised_get_jobs(tmp_path):
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        _call(server, "/jobs")
    records = _access_records(tmp_path, expect=1)
    assert records[-1] == {**records[-1], "method": "GET", "path": "/jobs", "status": 200}


def test_access_log_records_401_and_never_leaks_the_bad_token(tmp_path):
    fake_token = "definitely-not-the-real-token-xyz789"
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        status, _ = _call(server, "/jobs", token=fake_token)
    assert status == 401
    records = _access_records(tmp_path, expect=1)
    assert records[-1] == {**records[-1], "status": 401}
    raw = (Path(tmp_path) / "access.jsonl").read_text()
    assert fake_token not in raw


def test_access_log_never_persists_a_post_body_marker(tmp_path):
    marker = "marker-xyzzy-do-not-persist"
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        _call(
            server, "/jobs", method="POST",
            body={"mode": "deliver", "repo": "acme/mono", "title": "t",
                  "description": marker},
        )
    _access_records(tmp_path, expect=1)  # wait for the append before reading raw
    raw = (Path(tmp_path) / "access.jsonl").read_text()
    assert marker not in raw


def test_access_log_records_404_for_an_unknown_path(tmp_path):
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        _call(server, "/nope")
    records = _access_records(tmp_path, expect=1)
    assert records[-1] == {**records[-1], "method": "GET", "path": "/nope", "status": 404}


def test_access_log_write_failure_never_breaks_the_response(tmp_path, monkeypatch):
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        def boom(*args, **kwargs):
            raise OSError("disk gone")

        monkeypatch.setattr(server.dispatcher.access_log, "append", boom)
        status, payload = _call(server, "/health", token=None)
    assert status == 200
    assert payload["status"] == "ok"


def test_access_log_lives_at_jobs_root_and_is_created_lazily(tmp_path):
    target = Path(tmp_path) / "access.jsonl"
    assert not target.exists()
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        _call(server, "/health", token=None)
        _call(server, "/jobs", token=None)  # 401
        _call(server, "/nope")  # 404
        _call(server, "/jobs")  # 200
    assert target.exists()
    _access_records(tmp_path, expect=4)  # wait for all four appends to land
    lines = target.read_text().splitlines()
    assert len(lines) >= 4
    for line in lines:
        json.loads(line)  # every persisted line is valid, parseable JSON


def test_access_log_skips_a_connection_closed_before_any_response(tmp_path):
    # A client that opens a connection and closes it without sending
    # anything never reaches send_response, so handle_one_request's finally
    # block must see _access_log_status still None and skip the append —
    # never fabricate a record for a request that was never actually served.
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        host, port = server.httpd.server_address[:2]
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        status, payload = _call(server, "/health", token=None)
        assert status == 200
    records = _access_records(tmp_path, expect=1)
    assert [r["path"] for r in records] == ["/health"]


def test_access_log_concurrent_requests_never_lose_an_entry(tmp_path):
    with running(materialise=_mem_materialise, jobs_root=str(tmp_path)) as server:
        threads = [
            threading.Thread(target=lambda: _call(server, "/health", token=None))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    records = _access_records(tmp_path, expect=20, limit=1000)
    assert len(records) == 20
