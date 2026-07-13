"""Tests for the authenticated HTTP dispatch service."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from helpers import engine_responses
from test_assessment import assess_responses

from dev_team import __version__
from dev_team import dispatch as dispatch_mod
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
from dev_team.testing import ScriptedRunner

TOKEN = "s3cr3t-token"


def _mem_materialise(spec, dest):
    """A fake clone: no disk, no network — just a fresh in-memory workspace."""

    return InMemoryWorkspace()


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
        assert record.cost_usd == 0.0
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

    job_ws = InMemoryWorkspace()
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
    job_ws = InMemoryWorkspace()
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


def test_run_job_assess_with_backlog_but_no_dashboard_workspace():
    from dev_team.backlog import BacklogStore

    job_ws = InMemoryWorkspace()
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


def test_worker_is_single_flight_and_ordered():
    order = []
    first_in = threading.Event()
    release = threading.Event()

    def materialise(spec, dest):
        order.append(spec.id)
        if len(order) == 1:
            first_in.set()
            release.wait(5)  # hold job 1 so job 2 must wait its turn
        return InMemoryWorkspace()

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
        assert result == {
            "kind": "assess",
            "success": False,
            "error": job["error"],
            "cost_usd": 0,
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
        "id", "phase", "role", "claim", "evidence", "hash",
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
