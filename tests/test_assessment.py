"""Tests for the read-only assessment engine."""

from __future__ import annotations

import json

from helpers import run

from dev_team.assessment import (
    ASSESSMENT_JSON_PATH,
    CLASSIFICATIONS,
    AssessConfig,
    AssessmentEngine,
    AssessmentOutcome,
    BuildProbe,
    Component,
    InventoryStats,
    PhaseResult,
    ProbeCommandResult,
    _components_block,
    _effort_points,
    _mentions,
    audit_blind_spots,
    detect_components,
    dict_to_backlog,
    inventory_stats,
    outcome_to_backlog,
    outcome_to_dict,
    render_report,
    run_build_probe,
    scope_question,
)
from dev_team.budget import Budget
from dev_team.deadcode import DeadCodeFinding, DeadCodeReport
from dev_team.execution import (
    CommandResult,
    FakeCommandRunner,
    InMemoryWorkspace,
    LocalWorkspace,
)
from dev_team.profile import ProjectProfile
from dev_team.interaction import Reply, ScriptedChannel
from dev_team.persona import Roster
from dev_team.testing import ScriptedRunner, json_response


def inventory_dict(summary="a .NET monolith"):
    return {
        "summary": summary,
        "components": [
            {
                "name": "Backend",
                "path": "src/Api",
                "purpose": "HTTP API",
                "stack": ".NET Framework 4.7",
            },
            {"name": "Frontend", "path": "web", "purpose": "SPA"},
        ],
        "boundary": "REST between web/ and src/Api (evidence: web/src/api.js)",
        "entry_points": [{"path": "build.cake", "kind": "build"}],
        "findings": [
            {"claim": "CI config is dead", "evidence": ".teamcity/settings.kts"}
        ],
    }


def buildability_dict():
    return {
        "summary": "unlikely to build",
        "verdict": "unlikely",
        "blockers": [
            {
                "claim": "targets net47, needs Windows build chain",
                "evidence": "src/Api/Api.csproj",
                "category": "must-fix-to-build",
            }
        ],
        "runtime_requirements": [
            {"runtime": ".NET Framework", "required": "4.7", "evidence": "src/Api/Api.csproj"}
        ],
    }


def risk_dict():
    return {
        "summary": "several EOL dependencies",
        "dependencies": [
            {
                "name": "Newtonsoft.Json",
                "version": "9.0.1",
                "status": "known CVEs",
                "action": "must-fix",
                "evidence": "src/Api/packages.config",
            }
        ],
        "secrets": [{"claim": "connection string committed", "evidence": "Web.config"}],
        "data_layer": [{"claim": "EF6 migrations, last 2022", "evidence": "src/Api/Migrations"}],
        "external_services": [
            {"name": "payments API v1", "risk": "likely retired", "evidence": "src/Api/Pay.cs"}
        ],
    }


def coverage_dict():
    return {
        "summary": "tests exist but are stale",
        "tests": [{"claim": "MSTest suite, last touched 2021", "evidence": "tests/"}],
        "documentation": [{"claim": "README describes removed deploy path", "evidence": "README.md"}],
    }


def recommendation_dict(classification="dependency-surgery"):
    return {
        "summary": "revivable with dependency surgery",
        "classification": classification,
        "rationale": "core is sound; deps are the risk",
        "highest_risk": "unpinned NuGet feed for net47 packages",
        "plan": [
            {"step": "Pin build chain", "effort": "2 days", "detail": "global.json + CI"},
            {"step": "Upgrade Newtonsoft", "effort": "1 week", "detail": "CVE fix"},
        ],
    }


def conventions_dict():
    return {
        "conventions": [
            {
                "aspect": "naming",
                "convention": "PascalCase public members",
                "evidence": "src/Api/Program.cs",
            }
        ],
    }


def assess_responses(**overrides):
    """Keyed responses for every auditing role (override per test).

    The architect serves both the inventory and conventions phases (they are
    keyed by role), so its payload carries both phases' required keys.
    """

    payloads = {
        "software architect": {**inventory_dict(), **conventions_dict()},
        "DevOps engineer": buildability_dict(),
        "application security engineer": risk_dict(),
        "quality assurance engineer": coverage_dict(),
        "product manager": recommendation_dict(),
        "technical writer": {"summary": "Surgery, then revival."},
    }
    payloads.update(overrides)
    return {key: json_response(value) for key, value in payloads.items()}


def _workspace():
    return InMemoryWorkspace(
        {
            "MyApp.sln": "Microsoft Visual Studio Solution File",
            "src/Api/Api.csproj": "<Project><TargetFramework>net47</TargetFramework></Project>",
            "web/package.json": "{}",
            "README.md": "# MyApp",
        }
    )


def _engine(runner, workspace=None, **kwargs):
    kwargs.setdefault("workspace", workspace or _workspace())
    kwargs.setdefault("budget", Budget())
    return AssessmentEngine(runner, **kwargs)


# --- deterministic inventory ---------------------------------------------------


def test_inventory_stats_counts_loc_and_extensions():
    ws = InMemoryWorkspace(
        {
            "MyApp.sln": "a\nb",
            "src/Api/Program.cs": "x\ny\nz",
            "src/Api/Api.csproj": "<Project/>",
            ".dev_team/memory.json": "{}",
        }
    )
    stats = inventory_stats(ws)
    assert stats.total_files == 3  # bookkeeping excluded
    assert stats.loc_by_top == {"(root)": 2, "src": 4}
    assert stats.files_by_extension == {"sln": 1, "cs": 1, "csproj": 1}
    rendered = stats.render()
    assert "src=4" in rendered and "cs=1" in rendered


def test_inventory_stats_skips_unreadable_files(tmp_path):
    (tmp_path / "ok.cs").write_text("line\n")
    (tmp_path / "blob.dll").write_bytes(b"\xff\xfe\x00\x01binary")
    stats = inventory_stats(LocalWorkspace(str(tmp_path)))
    assert stats.unreadable_files == 1
    assert stats.loc_by_top == {"(root)": 2}
    assert "skipped: 1" in stats.render()


def test_inventory_stats_render_empty():
    stats = InventoryStats()
    assert "0 file(s)" in stats.render()


# --- happy path ------------------------------------------------------------------


def test_assess_records_transcripts_when_recorder_is_set():
    from dev_team.transcripts import TranscriptRecorder, list_transcripts

    runner = ScriptedRunner(by_system_prompt=assess_responses())
    tx = InMemoryWorkspace()
    recorder = TranscriptRecorder(tx, run="assess-x")
    engine = _engine(runner, transcript_recorder=recorder)
    run(engine.assess())
    # the audit's agents each left a captured transcript under their role/run
    assert list_transcripts(tx, "assess-x", "architect")


def test_assess_happy_path_produces_cited_report():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    engine = _engine(runner, workspace=ws)
    outcome = run(engine.assess())

    assert outcome.success is True
    assert outcome.aborted is False
    assert outcome.classification == "dependency-surgery"
    assert outcome.profile.kind == "dotnet"
    assert set(outcome.phases) == {
        "inventory", "buildability", "risk", "coverage", "recommendation",
        "conventions",
    }
    report = outcome.report_markdown
    assert "# Repository assessment" in report
    assert "**Classification: dependency-surgery**" in report
    assert "unpinned NuGet feed" in report
    assert "Surgery, then revival." in report          # writer's exec summary
    assert "src/Api/packages.config" in report          # citations survive
    assert "Builds today: unlikely" in report
    assert "1. Pin build chain — *2 days*. global.json + CI" in report
    assert "not a live vulnerability scan" in report
    # the report was written into the workspace at the default path
    assert ws.read_text("audit/assessment.md") == report


def test_assess_report_path_none_skips_writing():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    engine = _engine(runner, workspace=ws, config=AssessConfig(report_path=None))
    outcome = run(engine.assess())
    assert outcome.success is True
    assert "audit/assessment.md" not in ws.list_files()


def test_assess_is_read_only_apart_from_report():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    before = set(ws.list_files())
    engine = _engine(runner, workspace=ws)
    run(engine.assess())
    after = set(ws.list_files())
    # The report, the conventions profile, and the persisted structured
    # result are the only sanctioned writes.
    assert after - before == {
        "audit/assessment.md",
        ".dev_team/conventions.json",
        ASSESSMENT_JSON_PATH,
    }


def test_assess_agents_get_read_only_tools():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner)
    run(engine.assess())
    phase_calls = [c for c in runner.calls if c["prompt"].startswith("Phase ")]
    assert len(phase_calls) == 5
    for call in phase_calls:
        assert tuple(call["allowed_tools"]) == ("Read", "Grep", "Glob")
    # the writer only summarises — it gets no tools at all
    writer_calls = [c for c in runner.calls if "executive summary" in c["prompt"]]
    assert writer_calls and writer_calls[0]["allowed_tools"] is None


def test_assess_prompts_carry_evidence_and_discipline():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner)
    run(engine.assess())
    inventory_call = runner.calls[0]
    assert "MyApp.sln" in inventory_call["prompt"]           # repo context
    assert "Deterministic inventory" in inventory_call["prompt"]
    assert "state the ambiguity" in inventory_call["prompt"]
    risk_calls = [
        c for c in runner.calls
        if "application security engineer" in (c["system_prompt"] or "")
    ]
    assert "CVE knowledge comes from your training data" in risk_calls[0]["prompt"]


def test_assess_focus_reaches_every_phase():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, config=AssessConfig(focus="ignore the frontend"))
    outcome = run(engine.assess())
    assert outcome.focus == "ignore the frontend"
    phase_calls = [c for c in runner.calls if "audit" in c["prompt"].lower()]
    assert all("ignore the frontend" in c["prompt"] for c in phase_calls[:5])


def test_assess_events_and_personas():
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, listener=events.append)
    run(engine.assess())
    stages = {e.stage for e in events if e.role == "assessment"}
    assert {"start", "inventory", "risk", "report", "done"} <= stages
    assert any("Anders auditing" in e.message for e in events)


def test_assess_anonymous_roster_uses_roles():
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, roster=Roster.anonymous(), listener=events.append)
    outcome = run(engine.assess())
    assert outcome.success is True
    assert any("architect auditing" in e.message for e in events)


# --- degradation -----------------------------------------------------------------


def test_assess_phase_missing_keys_is_reported_not_fatal():
    responses = assess_responses(**{"DevOps engineer": {"summary": "no verdict"}})
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner)
    outcome = run(engine.assess())
    assert outcome.success is False
    build = outcome.phases["buildability"]
    assert build.ok is False
    assert "verdict" in build.error
    assert "Phase failed" in outcome.report_markdown
    # other phases still ran and are in the report
    assert outcome.phases["risk"].ok is True


def test_assess_unrecognised_classification_fails_phase():
    responses = assess_responses(
        **{"product manager": recommendation_dict(classification="rewrite-everything")}
    )
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner)
    outcome = run(engine.assess())
    assert outcome.success is False
    assert outcome.classification is None  # invalid value not surfaced as truth
    assert "rewrite-everything" in outcome.phases["recommendation"].error


def test_assess_agent_error_degrades_phase():
    responses = assess_responses(**{"software architect": "not json at all"})
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, config=AssessConfig(json_retries=0))
    outcome = run(engine.assess())
    assert outcome.phases["inventory"].ok is False
    assert outcome.success is False
    # downstream phases were told the inventory was unavailable
    later = [c for c in runner.calls if "Inventory summary" in c["prompt"]]
    assert later and all("unavailable" in c["prompt"] for c in later)


def test_assess_budget_exhaustion_degrades_gracefully():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, budget=Budget(limit_usd=0.0))
    outcome = run(engine.assess())
    assert outcome.success is False
    assert all(p.error == "budget exhausted" for p in outcome.phases.values())
    assert "# Repository assessment" in outcome.report_markdown


def test_assess_writer_failure_falls_back_to_recommendation_summary():
    responses = assess_responses(**{"technical writer": "garbage"})
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, config=AssessConfig(json_retries=0))
    outcome = run(engine.assess())
    assert outcome.executive_summary == "revivable with dependency surgery"


def test_assess_writer_empty_summary_falls_back():
    responses = assess_responses(**{"technical writer": {"summary": ""}})
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner)
    outcome = run(engine.assess())
    assert outcome.executive_summary == "revivable with dependency surgery"


# --- interactive scope check --------------------------------------------------------


def test_assess_scope_continue():
    channel = ScriptedChannel(script=[Reply(choice="continue")])
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, interaction=channel)
    outcome = run(engine.assess())
    assert outcome.success is True
    assert channel.questions[0].topic == "audit-scope"
    assert channel.questions[0].asked_by == "Anders"
    assert "a .NET monolith" in channel.questions[0].context


def test_assess_scope_focus_narrows_later_phases():
    channel = ScriptedChannel(script=[Reply(choice="focus", text="backend only")])
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, config=AssessConfig(focus="be quick"))
    engine.interaction = channel
    outcome = run(engine.assess())
    assert outcome.focus == "be quick; backend only"
    later = [c for c in runner.calls if "Phase 3" in c["prompt"]]
    assert later and "backend only" in later[0]["prompt"]


def test_assess_scope_focus_without_text_continues():
    channel = ScriptedChannel(script=[Reply(choice="focus", text="")])
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, interaction=channel)
    outcome = run(engine.assess())
    assert outcome.focus is None
    assert outcome.success is True


def test_assess_scope_abort_returns_partial_outcome():
    channel = ScriptedChannel(script=[Reply(choice="abort")])
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    engine = _engine(runner, workspace=ws, interaction=channel)
    outcome = run(engine.assess())
    assert outcome.aborted is True
    assert outcome.success is False
    assert set(outcome.phases) == {"inventory"}
    assert "aborted at the interactive scope check" in outcome.report_markdown
    assert outcome.report_path is None
    assert "audit/assessment.md" not in ws.list_files()  # nothing written


def test_assess_scope_skipped_when_inventory_failed():
    channel = ScriptedChannel()  # would raise if asked
    responses = assess_responses(**{"software architect": "broken"})
    runner = ScriptedRunner(by_system_prompt=responses)
    engine = _engine(runner, config=AssessConfig(json_retries=0))
    engine.interaction = channel
    outcome = run(engine.assess())
    assert channel.questions == []
    assert outcome.phases["inventory"].ok is False


def test_scope_question_defaults_to_continue():
    question = scope_question("summary", asked_by="Anders")
    assert question.default.key == "continue"
    assert question.find("focus").accepts_text is True


# --- serialisation ---------------------------------------------------------------


def test_outcome_to_dict_round_trip():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner)
    outcome = run(engine.assess())
    data = outcome_to_dict(outcome)
    assert data["success"] is True
    assert data["classification"] == "dependency-surgery"
    assert data["profile"]["kind"] == "dotnet"
    assert data["phases"]["risk"]["ok"] is True
    assert data["stats"]["total_files"] == 4
    assert data["report_markdown"].startswith("# Repository assessment")


def test_phase_result_ok_property():
    assert PhaseResult(phase="risk", role="security-engineer").ok is True
    assert PhaseResult(phase="risk", role="security-engineer", error="x").ok is False


def test_classification_none_when_recommendation_has_no_data():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, budget=Budget(limit_usd=0.0))
    outcome = run(engine.assess())
    assert outcome.classification is None


# --- renderer edge cases ----------------------------------------------------------


def test_render_report_with_sparse_data():
    """Every optional field absent: the renderer must not invent content."""

    from dev_team.assessment import AssessmentOutcome, render_report
    from dev_team.profile import ProjectProfile
    from dev_team.trace import Tracer

    phases = {
        # ok phases with minimal/malformed optional fields
        "inventory": PhaseResult(
            phase="inventory",
            role="architect",
            data={
                "summary": "",              # falsy summary skipped
                "components": "not-a-list", # _items returns []
                "findings": [],
                # boundary and entry_points absent entirely
            },
        ),
        "buildability": PhaseResult(
            phase="buildability",
            role="devops",
            data={"summary": "s", "verdict": "unknown", "blockers": [],
                  "runtime_requirements": [3, "junk"]},  # non-dict entries dropped
        ),
        "risk": PhaseResult(
            phase="risk",
            role="security-engineer",
            data={"summary": "s", "dependencies": [],
                  "secrets": [], "data_layer": [], "external_services": []},
        ),
        "coverage": PhaseResult(
            phase="coverage",
            role="qa",
            data={"summary": "s", "tests": [], "documentation": []},
        ),
        "recommendation": PhaseResult(
            phase="recommendation",
            role="product-manager",
            data={"summary": "s", "classification": "archive",
                  "highest_risk": "", "plan": [],
                  # rationale absent; highest_risk falsy; plan empty
                  },
        ),
    }
    outcome = AssessmentOutcome(
        profile=ProjectProfile(kind="unknown", verify_command=("pytest",)),
        stats=InventoryStats(total_files=1),
        phases=phases,
        executive_summary="",   # falsy -> section skipped
        report_markdown="",
        report_path=None,
        budget=Budget(),
        tracer=Tracer(),
    )
    report = render_report(outcome)
    assert "**Classification: archive**" in report
    assert "## Executive summary" not in report
    assert "Remediation plan" not in report
    assert "Highest-risk item" not in report
    assert "### Components" not in report
    assert "### Test infrastructure" not in report     # empty coverage lists
    assert "**Builds today: unknown**" in report
    assert "Runtime requirements" not in report        # only junk entries


def test_render_report_failed_phase_without_data_has_no_body():
    from dev_team.assessment import AssessmentOutcome, render_report
    from dev_team.profile import ProjectProfile
    from dev_team.trace import Tracer

    outcome = AssessmentOutcome(
        profile=ProjectProfile(kind="unknown", verify_command=("pytest",)),
        stats=InventoryStats(),
        phases={
            "risk": PhaseResult(phase="risk", role="security-engineer", error="boom")
        },
        executive_summary="",
        report_markdown="",
        report_path=None,
        budget=Budget(),
        tracer=Tracer(),
    )
    report = render_report(outcome)
    assert "_Phase failed (security-engineer): boom_" in report
    assert "### Dependencies" not in report


def test_render_report_failed_recommendation_hides_rejected_classification():
    """A recommendation phase that failed validation must not have its
    unvalidated classification surfaced as the audit's verdict."""

    from dev_team.assessment import AssessmentOutcome, render_report
    from dev_team.profile import ProjectProfile
    from dev_team.trace import Tracer

    outcome = AssessmentOutcome(
        profile=ProjectProfile(kind="unknown", verify_command=("pytest",)),
        stats=InventoryStats(),
        phases={
            # The validator rejected the phase (error set), but an out-of-contract
            # value still sits in data; the failure must win over it.
            "recommendation": PhaseResult(
                phase="recommendation",
                role="product-manager",
                data={"classification": "archive", "rationale": "unvetted"},
                error="rejected classification 'archive'",
            )
        },
        executive_summary="",
        report_markdown="",
        report_path=None,
        budget=Budget(),
        tracer=Tracer(),
    )
    # The verdict property is None for a failed phase...
    assert outcome.classification is None
    report = render_report(outcome)
    # ...and the rendered report must state the failure, not present the
    # rejected classification (nor its unvetted rationale) as the verdict.
    assert "## Recommendation" in report
    assert (
        "_Phase failed (product-manager): rejected classification 'archive'_"
        in report
    )
    assert "**Classification:" not in report
    assert "unvetted" not in report


def test_cited_handles_items_without_fields():
    from dev_team.assessment import _cited

    lines = []
    _cited(lines, [{"evidence": "a.cs"}, {}], "claim")
    assert lines == ["- (unspecified) (evidence: a.cs)", "- (unspecified)"]


# --- excludes, components, dead code, dependency scan ---------------------------


def test_inventory_stats_applies_exclude_globs():
    ws = InMemoryWorkspace(
        {
            "src/App.cs": "a\nb",
            "packages/Moq/Moq.dll": "binary",
            "App/bin/Debug/App.exe": "binary",
        }
    )
    stats = inventory_stats(ws, exclude_globs=("packages/*", "*/bin/*"))
    assert stats.total_files == 1
    assert stats.loc_by_top == {"src": 2}


def test_detect_components_and_block():
    ws = InMemoryWorkspace(
        {
            "package.json": "{}",
            "src/Api/Api.csproj": "<Project />",
            "src/Api/packages.config": "<packages />",
            "web/package.json": "{}",
            "vendored/dep/package.json": "{}",
        }
    )
    components = detect_components(ws, exclude_globs=("vendored/*",))
    assert [(c.name, c.path, c.manifest) for c in components] == [
        ("(root)", "", "package.json"),
        ("Api", "src/Api", "src/Api/Api.csproj"),
        ("web", "web", "web/package.json"),
    ]
    block = _components_block(components)
    assert "Detected components (3):" in block
    assert "- src/Api — manifest: src/Api/Api.csproj" in block
    assert "- (root) — manifest: package.json" in block
    assert _components_block([]) == ""


def test_components_block_caps_listing():
    components = [
        Component(name=f"c{i}", path=f"c{i}", manifest=f"c{i}/package.json")
        for i in range(35)
    ]
    block = _components_block(components)
    assert "... and 5 more" in block


_DEAD_WORKSPACE = {
    "MyApp.sln": 'Project("{G}") = "Api", "src\\Api\\Api.csproj", "{G2}"\nEndProject',
    "src/Api/Api.csproj": (
        '<Project ToolsVersion="12.0"><ItemGroup>'
        '<Compile Include="Program.cs" /></ItemGroup></Project>'
    ),
    "src/Api/Program.cs": "class P {}",
    "src/Api/Orphan.cs": "class O {}",
    "src/Api/packages.config": (
        '<packages><package id="Newtonsoft.Json" version="9.0.1" /></packages>'
    ),
    "README.md": "# MyApp",
}


def _vuln_fetch(payload):
    results = []
    for query in payload["queries"]:
        if query["package"]["name"] == "Newtonsoft.Json":
            results.append({"vulns": [{"id": "GHSA-5crp-9r3c-p9vr"}]})
        else:
            results.append({})
    return {"results": results}


def test_assess_integrates_dead_code_and_osv_scan():
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(
        runner,
        workspace=InMemoryWorkspace(dict(_DEAD_WORKSPACE)),
        listener=events.append,
        osv_fetch=_vuln_fetch,
    )
    outcome = run(engine.assess())
    assert outcome.success is True
    assert [f.path for f in outcome.dead_code.findings] == ["src/Api/Orphan.cs"]
    assert [v.id for v in outcome.dependency_scan.vulnerabilities] == [
        "GHSA-5crp-9r3c-p9vr"
    ]
    report = outcome.report_markdown
    assert "src/Api/Orphan.cs" in report
    assert "GHSA-5crp-9r3c-p9vr" in report
    assert "live" in report and "OSV.dev" in report
    stages = [e.stage for e in events]
    assert "dead-code" in stages and "dependencies" in stages
    data = outcome_to_dict(outcome)
    assert data["dead_code"]["findings"][0]["path"] == "src/Api/Orphan.cs"
    assert data["dependency_scan"]["vulnerabilities"][0]["id"] == "GHSA-5crp-9r3c-p9vr"
    assert data["conventions"]["conventions"]
    assert data["detected_components"]
    assert data["backlog_stories"] == []


def test_assess_can_disable_osv_scan():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(
        runner,
        workspace=InMemoryWorkspace(dict(_DEAD_WORKSPACE)),
        config=AssessConfig(osv_scan=False),
    )
    outcome = run(engine.assess())
    assert outcome.dependency_scan.queried is False
    assert "model knowledge, not a live vulnerability scan" in outcome.report_markdown


# --- component fan-out -----------------------------------------------------------


def test_component_fanout_deep_dives_each_component():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(
        runner, config=AssessConfig(component_fanout=True, max_components=12)
    )
    outcome = run(engine.assess())
    assert outcome.success is True
    result = outcome.phases["components"]
    assert result.ok
    entries = result.data["components"]
    assert {e["path"] for e in entries} == {"src/Api", "web"}
    assert all(e["findings"] for e in entries)
    assert "Component deep-dives" in outcome.report_markdown
    assert "### Api (`src/Api`)" in outcome.report_markdown


def test_component_fanout_caps_and_reports_skips():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(
        runner, config=AssessConfig(component_fanout=True, max_components=1)
    )
    outcome = run(engine.assess())
    summary = outcome.phases["components"].data["summary"]
    assert "1 component(s) audited" in summary
    assert "1 skipped (max_components=1)" in summary


def test_component_fanout_records_agent_failures():
    from dev_team.budget import BudgetExceededError

    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(
        runner, config=AssessConfig(component_fanout=True, json_retries=0)
    )
    real = engine.architect.ask_json
    calls = {"n": 0}

    async def sabotaged(prompt, **kwargs):
        if "component deep-dive" in prompt:
            calls["n"] += 1
            if calls["n"] == 1:
                raise BudgetExceededError(5.0, 5.0)
            from dev_team.errors import AgentResponseError

            raise AgentResponseError("architect", "not json")
        return await real(prompt, **kwargs)

    engine.architect.ask_json = sabotaged
    outcome = run(engine.assess())
    entries = outcome.phases["components"].data["components"]
    errors = sorted(e["error"] for e in entries)
    assert any("budget exhausted" in e for e in errors)
    assert any("unusable response" in e for e in errors)
    assert outcome.success is True  # advisory phase failures don't void the audit
    assert "_Deep-dive failed:" in outcome.report_markdown


# --- backlog bridge --------------------------------------------------------------


def _bare_outcome(phases=None, **overrides):
    from dev_team.profile import ProjectProfile
    from dev_team.trace import Tracer

    defaults = dict(
        profile=ProjectProfile(kind="dotnet-framework", verify_command=None),
        stats=InventoryStats(),
        phases=phases or {},
        executive_summary="",
        report_markdown="",
        report_path=None,
        budget=Budget(),
        tracer=Tracer(),
    )
    defaults.update(overrides)
    return AssessmentOutcome(**defaults)


def test_effort_points_mapping():
    assert _effort_points("about 2 months") == 13
    assert _effort_points("1 week") == 8
    assert _effort_points("3 days") == 3
    assert _effort_points("a few hours") == 2
    assert _effort_points("") == 2


def _findings_outcome():
    """A bare outcome rich in convertible findings (and junk to skip)."""

    from dev_team.deadcode import DeadCodeFinding, DeadCodeReport
    from dev_team.depscan import Dependency, DependencyScan, Vulnerability

    phases = {
        "recommendation": PhaseResult(
            phase="recommendation",
            role="product-manager",
            data={
                "classification": "dependency-surgery",
                "plan": [
                    {"step": "Pin build chain", "effort": "2 days", "detail": "CI"},
                    {"step": "", "effort": "1 week"},
                    "junk",
                ],
            },
        ),
        "buildability": PhaseResult(
            phase="buildability",
            role="devops",
            data={
                "blockers": [
                    {"claim": "needs Windows", "category": "must-fix-to-build",
                     "evidence": "App.csproj"},
                    {"claim": "old npm", "category": "will-bite-later"},
                    {"claim": "", "category": "must-fix-to-build"},
                    "junk",
                ]
            },
        ),
        "risk": PhaseResult(
            phase="risk",
            role="security-engineer",
            data={
                "dependencies": [
                    {"name": "Moq", "action": "must-fix", "version": "4.2"},
                    {"name": "xunit", "action": "ok"},
                    "junk",
                ],
                "secrets": [
                    {"claim": "license committed", "evidence": "Aspose.lic"},
                    {"claim": ""},
                    "junk",
                ],
            },
        ),
    }
    dead = DeadCodeReport(
        findings=[
            DeadCodeFinding("unreferenced-sources", f"App/Dead{i}.cs", "unused")
            for i in range(25)
        ]
        + [DeadCodeFinding("orphaned-projects", "Old/Old.csproj", "orphan")],
        probes_run=["unreferenced-sources", "orphaned-projects"],
    )
    dep = Dependency("Moq", "4.2.1409.1722", "NuGet", "App/packages.config")
    scan = DependencyScan(
        dependencies=[dep],
        vulnerabilities=[Vulnerability("GHSA-1", dep)],
        queried=True,
    )
    return _bare_outcome(phases=phases, dead_code=dead, dependency_scan=scan)


#: What _findings_outcome converts to, in order.
_FINDINGS_TITLES = [
    "Pin build chain",
    "Fix build blocker: needs Windows",
    "Upgrade or replace dependency Moq",
    "Remove hardcoded secret: license committed",
    "Remove dead code (orphaned-projects: 1 path(s))",
    "Remove dead code (unreferenced-sources: 25 path(s))",
    "Patch Moq 4.2.1409.1722: GHSA-1",
]


def test_outcome_to_backlog_converts_findings_to_stories():
    from dev_team.backlog import Backlog

    outcome = _findings_outcome()
    backlog = Backlog()
    stories = outcome_to_backlog(outcome, backlog)
    titles = [s.title for s in stories]
    assert titles == _FINDINGS_TITLES
    assert backlog.epics[0].title == "Assessment remediation"
    assert "dependency-surgery" in backlog.epics[0].description
    by_title = {s.title: s for s in stories}
    assert by_title["Pin build chain"].estimate == 3
    assert by_title["Remove hardcoded secret: license committed"].estimate == 1
    assert " …" in by_title["Remove dead code (unreferenced-sources: 25 path(s))"].description

    # Re-running dedupes by title and reuses the epic.
    again = outcome_to_backlog(outcome, backlog)
    assert again == []
    assert len(backlog.epics) == 1


def test_outcome_to_backlog_skips_failed_phases():
    from dev_team.backlog import Backlog

    phases = {
        "recommendation": PhaseResult(
            phase="recommendation", role="pm", error="budget", data={"plan": [{"step": "X"}]}
        ),
        "buildability": PhaseResult(
            phase="buildability", role="devops", error="bad json",
            data={"blockers": [{"claim": "Y", "category": "must-fix-to-build"}]},
        ),
    }
    assert outcome_to_backlog(_bare_outcome(phases=phases), Backlog()) == []


def test_dict_to_backlog_matches_outcome_to_backlog():
    """The dict core is lossless: both entry points yield identical stories."""

    from dev_team.backlog import Backlog

    outcome = _findings_outcome()
    from_outcome = outcome_to_backlog(outcome, Backlog())
    from_dict = dict_to_backlog(outcome_to_dict(outcome), Backlog())
    assert from_dict == from_outcome
    # the empty-findings edge agrees too
    assert dict_to_backlog(outcome_to_dict(_bare_outcome()), Backlog()) == []


def test_dict_to_backlog_from_a_json_round_trip():
    """The persisted-then-reloaded JSON produces the same backlog."""

    from dev_team.backlog import Backlog

    outcome = _findings_outcome()
    payload = json.loads(json.dumps(outcome_to_dict(outcome)))
    backlog = Backlog()
    stories = dict_to_backlog(payload, backlog)
    assert [s.title for s in stories] == _FINDINGS_TITLES
    assert backlog.epics[0].title == "Assessment remediation"
    assert "dependency-surgery" in backlog.epics[0].description
    # regenerating from the same payload dedupes by title
    assert dict_to_backlog(payload, backlog) == []


def test_dict_to_backlog_per_repo_epics_and_scoped_dedup():
    """With a repo, each repository gets its own epic and its own dedup scope."""

    from dev_team.backlog import Backlog

    data = json.loads(json.dumps(outcome_to_dict(_findings_outcome())))
    backlog = Backlog()
    first = dict_to_backlog(data, backlog, repo="acme/one", source_job="assess-1")
    assert [s.title for s in first] == _FINDINGS_TITLES
    assert backlog.epics[0].title == "Remediation — acme/one"
    assert backlog.epics[0].description == (
        "From assessment of acme/one (classification: dependency-surgery)"
    )
    # A DIFFERENT repo gets its own epic: identical finding titles under
    # another repo's epic are new stories, never suppressed.
    second = dict_to_backlog(data, backlog, repo="acme/two", source_job="assess-2")
    assert [s.title for s in second] == _FINDINGS_TITLES
    assert [e.title for e in backlog.epics] == [
        "Remediation — acme/one", "Remediation — acme/two",
    ]
    assert all(s.epic_id == backlog.epics[1].id for s in second)
    # Re-assessing the SAME repo refreshes its epic: dedup within, no flood.
    assert dict_to_backlog(data, backlog, repo="acme/one", source_job="assess-3") == []
    assert len(backlog.epics) == 2
    assert len(backlog.stories) == 2 * len(_FINDINGS_TITLES)
    # A findings-free assessment of a repo still names its classification.
    bare = Backlog()
    assert dict_to_backlog(
        outcome_to_dict(_bare_outcome()), bare, repo="acme/empty"
    ) == []
    assert bare.epics[0].description == (
        "From assessment of acme/empty (classification: unclassified)"
    )


def test_dict_to_backlog_threads_finding_provenance():
    """LLM-finding stories carry list_findings' exact ids; deterministic don't."""

    from dev_team.assessment import list_findings
    from dev_team.backlog import Backlog

    data = outcome_to_dict(_findings_outcome())
    stories = dict_to_backlog(data, Backlog(), repo="acme/one", source_job="assess-1")
    by_title = {s.title: s for s in stories}
    assert by_title["Pin build chain"].finding_id == "recommendation.plan[0]"
    assert (
        by_title["Fix build blocker: needs Windows"].finding_id
        == "buildability.blockers[0]"
    )
    assert (
        by_title["Upgrade or replace dependency Moq"].finding_id
        == "risk.dependencies[0]"
    )
    assert (
        by_title["Remove hardcoded secret: license committed"].finding_id
        == "risk.secrets[0]"
    )
    # Every threaded id resolves against the finding enumerator — the two
    # schemes cannot drift, so `--verify` / dev_team_verify can re-check it.
    known = {f["id"] for f in list_findings(data)}
    assert {s.finding_id for s in stories if s.finding_id is not None} <= known
    assert all(s.source_job == "assess-1" for s in stories)
    # Deterministic findings are exact program output, not model claims.
    for title in (
        "Remove dead code (orphaned-projects: 1 path(s))",
        "Remove dead code (unreferenced-sources: 25 path(s))",
        "Patch Moq 4.2.1409.1722: GHSA-1",
    ):
        assert by_title[title].finding_id is None


def test_outcome_to_backlog_keeps_the_single_epic_without_repo_context():
    """The wrapper stays back-compatible: no repo, no source job."""

    from dev_team.backlog import Backlog

    backlog = Backlog()
    stories = outcome_to_backlog(_findings_outcome(), backlog)
    assert backlog.epics[0].title == "Assessment remediation"
    assert all(s.source_job is None for s in stories)
    # finding ids are intrinsic to the finding, so they thread regardless
    assert {s.finding_id for s in stories} > {None}


# --- persisted structured result ---------------------------------------------------


def test_assess_persists_structured_result():
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    engine = _engine(runner, workspace=ws, listener=events.append)
    outcome = run(engine.assess())
    assert ASSESSMENT_JSON_PATH in ws.list_files()
    data = json.loads(ws.read_text(ASSESSMENT_JSON_PATH))
    # exactly the outcome_to_dict shape (modulo JSON's tuple->list coercion)
    assert data == json.loads(json.dumps(outcome_to_dict(outcome)))
    assert data["classification"] == "dependency-surgery"
    assert any(e.stage == "persist" for e in events)


def test_assess_persist_result_can_be_disabled():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    engine = _engine(runner, workspace=ws, config=AssessConfig(persist_result=False))
    outcome = run(engine.assess())
    assert outcome.success is True
    assert ASSESSMENT_JSON_PATH not in ws.list_files()


def test_persisted_result_regenerates_the_backlog_later_without_agents():
    """Assess once (persist on), then build the backlog purely from disk."""

    from dev_team.backlog import Backlog, BacklogStore

    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = InMemoryWorkspace(dict(_DEAD_WORKSPACE))
    engine = _engine(runner, workspace=ws, osv_fetch=_vuln_fetch)
    outcome = run(engine.assess())
    assert ".dev_team/backlog.json" not in ws.list_files()  # update_backlog off

    # Later — no engine, no runner: read the persisted JSON, merge, save.
    data = json.loads(ws.read_text(ASSESSMENT_JSON_PATH))
    store = BacklogStore(ws)
    backlog = store.load()
    stories = dict_to_backlog(data, backlog)
    store.save(backlog)
    inline = outcome_to_backlog(outcome, Backlog())
    assert [s.title for s in stories] == [s.title for s in inline]
    assert stories, "the fixture repository yields remediation stories"
    assert {s.title for s in store.load().stories} == {s.title for s in stories}


def test_assess_update_backlog_persists_stories():
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = InMemoryWorkspace(dict(_DEAD_WORKSPACE))
    engine = _engine(
        runner,
        workspace=ws,
        config=AssessConfig(update_backlog=True),
        listener=events.append,
        osv_fetch=_vuln_fetch,
    )
    outcome = run(engine.assess())
    assert outcome.backlog_stories
    assert ".dev_team/backlog.json" in ws.list_files()
    assert any(e.stage == "backlog" for e in events)

    from dev_team.backlog import BacklogStore

    stored = BacklogStore(ws).load()
    assert {s.title for s in stored.stories} >= set(outcome.backlog_stories)


def test_update_backlog_without_stories_writes_nothing():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = InMemoryWorkspace({"README.md": "# empty"})
    engine = _engine(runner, workspace=ws)
    outcome = _bare_outcome()
    engine._update_backlog(outcome)
    assert outcome.backlog_stories == []
    assert ".dev_team/backlog.json" not in ws.list_files()


# --- conventions capture ----------------------------------------------------------


def test_assess_persists_conventions_profile():
    from dev_team.conventions import ConventionsStore

    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    ws.write_text(".editorconfig", "root = true")
    engine = _engine(runner, workspace=ws)
    outcome = run(engine.assess())
    assert outcome.conventions is not None
    assert outcome.conventions.sources == [".editorconfig"]
    stored = ConventionsStore(ws).load()
    assert stored is not None
    assert stored.conventions[0]["aspect"] == "naming"
    assert "House conventions" in outcome.report_markdown
    assert "`.editorconfig`" in outcome.report_markdown


def test_persist_conventions_skips_missing_failed_or_empty():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner)
    assert engine._persist_conventions({}, []) is None
    failed = {"conventions": PhaseResult(phase="conventions", role="architect", error="x")}
    assert engine._persist_conventions(failed, []) is None
    empty = {
        "conventions": PhaseResult(
            phase="conventions", role="architect",
            data={"summary": "", "conventions": []},
        )
    }
    assert engine._persist_conventions(empty, []) is None


def test_persist_conventions_can_be_disabled():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = _workspace()
    engine = _engine(runner, workspace=ws, config=AssessConfig(save_conventions=False))
    outcome = run(engine.assess())
    assert outcome.conventions is not None
    assert ".dev_team/conventions.json" not in ws.list_files()


def test_report_renders_failed_conventions_phase():
    phases = {
        "conventions": PhaseResult(
            phase="conventions", role="architect", error="ran out of budget"
        )
    }
    report = render_report(_bare_outcome(phases=phases))
    assert "## House conventions" in report
    assert "_Phase failed (architect): ran out of budget_" in report


def test_detect_components_one_per_directory():
    ws = InMemoryWorkspace({"dual/package.json": "{}", "dual/pyproject.toml": ""})
    components = detect_components(ws)
    assert len(components) == 1
    assert components[0].manifest == "dual/package.json"


def test_assess_with_no_dead_code_emits_no_event():
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(
        runner,
        workspace=InMemoryWorkspace({"pyproject.toml": "", "src/app.py": "x = 1"}),
        listener=events.append,
    )
    outcome = run(engine.assess())
    assert outcome.dead_code.findings == []
    assert not any(e.stage == "dead-code" for e in events)


def test_report_renders_component_entry_without_summary_or_findings():
    phases = {
        "components": PhaseResult(
            phase="components",
            role="architect",
            data={
                "summary": "1 component(s) audited in parallel",
                "components": [{"name": "web", "path": "web", "summary": "", "findings": []}],
            },
        )
    }
    report = render_report(_bare_outcome(phases=phases))
    assert "### web (`web`)" in report
    assert "_Deep-dive failed" not in report


# --- rebuild classification -------------------------------------------------------


def test_rebuild_is_a_first_class_classification():
    assert "rebuild" in CLASSIFICATIONS
    responses = assess_responses(
        **{"product manager": recommendation_dict("rebuild")}
    )
    engine = _engine(ScriptedRunner(by_system_prompt=responses))
    outcome = run(engine.assess())
    assert outcome.success is True
    assert outcome.classification == "rebuild"
    assert "**Classification: rebuild**" in outcome.report_markdown


# --- build probe ------------------------------------------------------------------


_NODE_PROFILE = ProjectProfile(
    kind="node",
    verify_command=("npm", "test"),
    setup_command=("npm", "install"),
)


def test_run_build_probe_needs_a_real_directory():
    probe = run_build_probe(_NODE_PROFILE, None, "/repo", timeout=60.0)
    assert probe.requested and not probe.ran
    assert "no real workspace directory" in probe.skipped_reason
    probe = run_build_probe(_NODE_PROFILE, FakeCommandRunner(), None, timeout=60.0)
    assert "no real workspace directory" in probe.skipped_reason
    assert probe.succeeded is None
    assert "skipped" in probe.render()


def test_run_build_probe_skips_profiles_with_no_commands():
    profile = ProjectProfile(
        kind="dotnet-framework", verify_command=None, locally_runnable=False
    )
    probe = run_build_probe(profile, FakeCommandRunner(), "/repo", timeout=60.0)
    assert not probe.ran
    assert "dotnet-framework profile proposes no locally runnable" in probe.skipped_reason


def test_run_build_probe_green_runs_setup_then_verify():
    runner = FakeCommandRunner()
    runner.add_rule("npm test", CommandResult(["npm", "test"], 0, "42 passing", ""))
    probe = run_build_probe(_NODE_PROFILE, runner, "/repo", timeout=60.0)
    assert probe.ran and probe.succeeded is True
    assert [c.command for c in probe.commands] == [("npm", "install"), ("npm", "test")]
    assert probe.not_run == []
    assert runner.calls == [["npm", "install"], ["npm", "test"]]
    rendered = probe.render()
    assert "`npm install` — ok" in rendered
    assert "`npm test` — ok" in rendered
    assert "42 passing" in rendered  # output tail survives as evidence


def test_run_build_probe_stops_at_first_failure():
    runner = FakeCommandRunner()
    runner.add_rule(
        "npm install", CommandResult(["npm", "install"], 1, "", "ERESOLVE unable to resolve")
    )
    probe = run_build_probe(_NODE_PROFILE, runner, "/repo", timeout=60.0)
    assert probe.ran and probe.succeeded is False
    assert [c.command for c in probe.commands] == [("npm", "install")]
    assert probe.not_run == [("npm", "test")]
    assert runner.calls == [["npm", "install"]]  # verify never ran
    rendered = probe.render()
    assert "`npm install` — FAILED (exit 1)" in rendered
    assert "ERESOLVE" in rendered
    assert "`npm test` — not run (a previous command failed)" in rendered


def test_run_build_probe_verify_only_profile_and_output_truncation():
    profile = ProjectProfile(kind="rust", verify_command=("cargo", "test"))
    runner = FakeCommandRunner()
    runner.add_rule(
        "cargo test", CommandResult(["cargo", "test"], 0, "x" * 5_000, "")
    )
    probe = run_build_probe(profile, runner, "/repo", timeout=60.0)
    assert [c.command for c in probe.commands] == [("cargo", "test")]
    assert len(probe.commands[0].output_tail) == 4_000


def test_build_probe_render_and_dict_when_never_requested():
    probe = BuildProbe()
    assert probe.render() == ""
    assert probe.ran is False and probe.succeeded is None
    payload = probe.to_dict()
    assert payload["requested"] is False
    assert payload["ran"] is False
    assert payload["succeeded"] is None


def test_build_probe_to_dict_round_trips_commands():
    probe = BuildProbe(
        requested=True,
        commands=[ProbeCommandResult(("npm", "install"), 1, "boom")],
        not_run=[("npm", "test")],
    )
    payload = probe.to_dict()
    assert payload["succeeded"] is False
    assert payload["commands"] == [
        {"command": ["npm", "install"], "exit_code": 1, "output_tail": "boom"}
    ]
    assert payload["not_run"] == [["npm", "test"]]


def test_build_probe_command_without_output_renders_no_tail():
    probe = BuildProbe(
        requested=True, commands=[ProbeCommandResult(("npm", "test"), 0, "")]
    )
    assert "output tail" not in probe.render()


def _node_repo(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "app", "version": "1.0.0"}')
    (tmp_path / "index.js").write_text("module.exports = 1;\n")
    return tmp_path


def test_assess_build_probe_feeds_evidence_and_report(tmp_path):
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    command_runner = FakeCommandRunner()
    command_runner.add_rule(
        "npm test", CommandResult(["npm", "test"], 1, "", "Error: Cannot find module")
    )
    engine = AssessmentEngine(
        runner,
        workspace=LocalWorkspace(str(_node_repo(tmp_path))),
        config=AssessConfig(build_probe=True),
        budget=Budget(),
        listener=events.append,
        command_runner=command_runner,
    )
    outcome = run(engine.assess())
    assert outcome.build_probe.ran and outcome.build_probe.succeeded is False
    assert ["npm", "install"] in command_runner.calls
    # real results reach the auditors as evidence...
    buildability_call = next(
        c for c in runner.calls if "BUILDABILITY" in c["prompt"]
    )
    assert "Build probe" in buildability_call["prompt"]
    assert "Cannot find module" in buildability_call["prompt"]
    # ...and the report appendix.
    assert "Build probe" in outcome.report_markdown
    assert any(e.stage == "build-probe" and "red" in e.message for e in events)
    assert outcome_to_dict(outcome)["build_probe"]["succeeded"] is False


def test_assess_build_probe_green_event(tmp_path):
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = AssessmentEngine(
        runner,
        workspace=LocalWorkspace(str(_node_repo(tmp_path))),
        config=AssessConfig(build_probe=True),
        budget=Budget(),
        listener=events.append,
        command_runner=FakeCommandRunner(),
    )
    outcome = run(engine.assess())
    assert outcome.build_probe.succeeded is True
    assert any(e.stage == "build-probe" and "green" in e.message for e in events)


def test_assess_build_probe_skipped_without_real_directory():
    events = []
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = AssessmentEngine(
        runner,
        workspace=_workspace(),  # in-memory: no directory to run commands in
        config=AssessConfig(build_probe=True),
        budget=Budget(),
        listener=events.append,
        command_runner=FakeCommandRunner(),
    )
    outcome = run(engine.assess())
    assert not outcome.build_probe.ran
    assert "no real workspace directory" in outcome.build_probe.skipped_reason
    assert any(e.stage == "build-probe" and "skipped" in e.message for e in events)
    assert "Build probe: skipped" in outcome.report_markdown


def test_assess_default_never_runs_build_commands():
    command_runner = FakeCommandRunner()
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, command_runner=command_runner)
    outcome = run(engine.assess())
    assert outcome.build_probe.requested is False
    assert command_runner.calls == []  # in-memory workspace: not even git
    assert "Build probe" not in outcome.report_markdown


# --- audit blind spots ------------------------------------------------------------


def test_mentions_tokenises_citations():
    assert _mentions("see src/Api/Program.cs", "src")
    assert _mentions("(evidence: `web/app.js`)", "web")
    assert _mentions("the src directory.", "src")
    assert not _mentions("sources are unclear", "src")
    assert not _mentions("api/src/thing", "src")


def test_audit_blind_spots_names_uncited_directories():
    stats = InventoryStats(loc_by_top={"src": 100, "legacy": 50, "(root)": 5})
    phases = {
        "inventory": PhaseResult(
            phase="inventory",
            role="architect",
            data={
                "findings": [{"claim": "x", "evidence": "src/App.cs"}],
                "count": 3,  # non-citation leaves are ignored
            },
        )
    }
    assert audit_blind_spots(stats, phases, DeadCodeReport()) == ["legacy"]


def test_audit_blind_spots_counts_path_keys_and_dead_code():
    stats = InventoryStats(
        loc_by_top={"web": 10, "Sleepy": 20, "tools": 30, "(root)": 1}
    )
    phases = {
        "inventory": PhaseResult(
            phase="inventory",
            role="architect",
            data={
                "components": [{"name": "SPA", "path": "web", "purpose": "ui"}],
                "boundary": {"evidence": ["tools/build.sh"]},  # non-str evidence: recursed
            },
        )
    }
    dead = DeadCodeReport(
        findings=[DeadCodeFinding(probe="dormant-directories", path="Sleepy", detail="old")]
    )
    assert audit_blind_spots(stats, phases, dead) == []
    assert audit_blind_spots(stats, {}, DeadCodeReport()) == ["Sleepy", "tools", "web"]


def test_report_names_blind_spots():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    ws = InMemoryWorkspace(
        {
            "MyApp.sln": "Microsoft Visual Studio Solution File",
            "src/Api/Api.csproj": "<Project><TargetFramework>net47</TargetFramework></Project>",
            "web/package.json": "{}",
            "uncharted/blob.py": "x = 1\n",
        }
    )
    engine = _engine(runner, workspace=ws)
    outcome = run(engine.assess())
    assert outcome.blind_spots == ["uncharted"]
    assert "Audit blind spots" in outcome.report_markdown
    assert "`uncharted/`" in outcome.report_markdown
    assert outcome_to_dict(outcome)["blind_spots"] == ["uncharted"]


def test_report_omits_blind_spots_when_everything_was_cited():
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    outcome = run(_engine(runner).assess())
    assert outcome.blind_spots == []
    assert "Audit blind spots" not in outcome.report_markdown


def test_audit_blind_spots_ignores_named_entries():
    stats = InventoryStats(loc_by_top={"audit": 5, "src": 10, "(root)": 1})
    assert audit_blind_spots(stats, {}, DeadCodeReport(), ignore=("audit",)) == ["src"]


def test_reassessment_does_not_flag_its_own_report_directory():
    ws = _workspace()
    ws.write_text("audit/assessment.md", "# a previous run's report")
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    outcome = run(_engine(runner, workspace=ws).assess())
    assert outcome.blind_spots == []


def test_root_level_report_path_ignores_nothing():
    ws = _workspace()
    ws.write_text("audit/assessment.md", "# a previous run's report")
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    engine = _engine(runner, workspace=ws, config=AssessConfig(report_path="report.md"))
    outcome = run(engine.assess())
    # with the report at the root, the leftover audit/ dir is a real blind spot
    assert outcome.blind_spots == ["audit"]


# --- finding enumeration + re-verification ------------------------------------


def _verifiable_assessment():
    """A serialised assessment rich in enumerable claims (and junk to skip)."""

    return {
        "phases": {
            "inventory": {
                "role": "architect", "ok": True, "error": None,
                "data": {"findings": [
                    {"claim": "CI config is dead", "evidence": ".teamcity/settings.kts"},
                    {"note": "no claim text"},   # skipped: no claim field
                    "junk",                      # skipped: not a dict
                ]},
            },
            "buildability": {
                "role": "devops", "ok": False, "error": "budget exhausted",
                "data": {"blockers": [
                    {"claim": "never enumerated", "category": "must-fix-to-build"}
                ]},
            },
            "risk": {
                "role": "security-engineer", "ok": True, "error": None,
                "data": {
                    "dependencies": [
                        {"name": "Moq", "version": "4.2", "action": "must-fix",
                         "evidence": "packages.config"}
                    ],
                    "secrets": [
                        {"claim": "connection string committed",
                         "evidence": "Web.config"}
                    ],
                    "data_layer": [],
                    "external_services": [
                        {"name": "payments API v1", "risk": "retired",
                         "evidence": ["Pay.cs"]}  # non-string evidence -> ""
                    ],
                },
            },
            "coverage": {
                "role": "qa", "ok": True, "error": None,
                "data": {
                    "tests": [{"claim": "MSTest suite stale", "evidence": "tests/"}],
                    "documentation": [
                        {"claim": "README wrong", "evidence": "README.md"}
                    ],
                },
            },
            "conventions": {
                "role": "architect", "ok": True, "error": None,
                "data": {"conventions": [
                    {"aspect": "naming", "convention": "PascalCase everywhere",
                     "evidence": "Program.cs"}
                ]},
            },
            "recommendation": {
                "role": "product-manager", "ok": True, "error": None,
                "data": {"plan": [
                    {"step": "Pin build chain", "effort": "2 days", "detail": "CI"}
                ]},
            },
            "components": {
                "role": "architect", "ok": True, "error": None,
                "data": {"components": [
                    {"name": "Api", "path": "src/Api", "findings": [
                        {"claim": "God object in Pay.cs", "evidence": "src/Api/Pay.cs"}
                    ]},
                    # a nested item with no claim text is skipped, not surfaced
                    {"name": "web", "path": "web", "findings": [{"claim": "  "}]},
                ]},
            },
        },
        # deterministic outputs: never enumerated as re-verifiable claims
        "dead_code": {"findings": [{"probe": "x", "path": "Dead.cs"}]},
        "dependency_scan": {"vulnerabilities": [{"id": "GHSA-1"}]},
    }


def test_list_findings_enumerates_llm_phases_with_positional_ids():
    from dev_team.assessment import list_findings

    findings = list_findings(_verifiable_assessment())
    by_id = {f["id"]: f for f in findings}
    assert set(by_id) == {
        "inventory.findings[0]",
        "risk.dependencies[0]",
        "risk.secrets[0]",
        "risk.external_services[0]",
        "coverage.tests[0]",
        "coverage.documentation[0]",
        "conventions.conventions[0]",
        "recommendation.plan[0]",
        "components.components[0].findings[0]",
    }
    # claim text comes from the phase's own field: claim/step/name/convention
    assert by_id["risk.dependencies[0]"]["claim"] == "Moq"
    assert by_id["recommendation.plan[0]"]["claim"] == "Pin build chain"
    assert by_id["conventions.conventions[0]"]["claim"] == "PascalCase everywhere"
    assert by_id["risk.secrets[0]"]["claim"] == "connection string committed"
    # role + evidence travel with the finding; non-string evidence degrades
    assert by_id["risk.secrets[0]"]["role"] == "security-engineer"
    assert by_id["risk.secrets[0]"]["evidence"] == "Web.config"
    assert by_id["risk.external_services[0]"]["evidence"] == ""
    # the components nesting is flattened with its own id shape
    assert by_id["components.components[0].findings[0]"]["phase"] == "components"
    assert by_id["components.components[0].findings[0]"]["role"] == "architect"


def test_list_findings_skips_failed_phases_and_deterministic_outputs():
    from dev_team.assessment import list_findings

    findings = list_findings(_verifiable_assessment())
    claims = [f["claim"] for f in findings]
    assert "never enumerated" not in claims          # buildability ok:false
    assert all("dead_code" not in f["id"] for f in findings)
    assert all("dependency_scan" not in f["id"] for f in findings)
    assert list_findings({}) == []                   # no phases at all


def test_list_findings_hash_is_short_and_stable():
    import hashlib

    from dev_team.assessment import list_findings

    first = list_findings(_verifiable_assessment())
    second = list_findings(_verifiable_assessment())
    by_id = {f["id"]: f for f in first}
    plan = by_id["recommendation.plan[0]"]
    expected = hashlib.sha256("Pin build chain".encode("utf-8")).hexdigest()[:12]
    assert plan["hash"] == expected
    assert len(plan["hash"]) == 12
    assert [f["hash"] for f in first] == [f["hash"] for f in second]


def test_list_findings_from_a_real_assessment_run():
    from dev_team.assessment import list_findings

    runner = ScriptedRunner(by_system_prompt=assess_responses())
    outcome = run(_engine(runner).assess())
    payload = json.loads(json.dumps(outcome_to_dict(outcome)))  # disk round-trip
    by_id = {f["id"]: f for f in list_findings(payload)}
    assert by_id["inventory.findings[0]"]["claim"] == "CI config is dead"
    assert by_id["risk.secrets[0]"]["claim"] == "connection string committed"
    assert "buildability.blockers[0]" in by_id


def test_find_finding_by_exact_id_then_claim_substring():
    from dev_team.assessment import find_finding

    data = _verifiable_assessment()
    assert find_finding(data, "risk.secrets[0]")["claim"] == (
        "connection string committed"
    )
    # case-insensitive substring of the claim text
    assert find_finding(data, "CONNECTION string")["id"] == "risk.secrets[0]"
    # first match (enumeration order) wins for an ambiguous substring
    assert find_finding(data, "c")["id"] == "inventory.findings[0]"
    assert find_finding(data, "no such finding anywhere") is None
    assert find_finding(data, "   ") is None
    assert find_finding({}, "anything") is None


def _security_verdict(payload):
    from dev_team.testing import json_response as _json_response

    return ScriptedRunner(
        by_system_prompt={"application security engineer": _json_response(payload)}
    )


def _finding_fixture(**overrides):
    finding = {
        "id": "risk.secrets[0]",
        "phase": "risk",
        "role": "security-engineer",
        "claim": "connection string committed",
        "evidence": "Web.config",
        "hash": "abc123abc123",
    }
    finding.update(overrides)
    return finding


def test_verify_finding_confirmed_happy_path():
    from dev_team.assessment import verify_finding

    runner = _security_verdict(
        {
            "verdict": "confirmed",
            "rationale": "read Web.config; the credential is on line 12",
            "citations": [
                {"path": "Web.config", "note": "line 12"},
                "junk",          # non-dict citation dropped
                {"path": 3},     # coerced, note defaults
            ],
        }
    )
    result = run(
        verify_finding(
            runner, InMemoryWorkspace(), _finding_fixture(), source_job="assess-1"
        )
    )
    assert result["success"] is True
    assert result["verdict"] == "confirmed"
    assert result["rationale"].startswith("read Web.config")
    assert result["citations"] == [
        {"path": "Web.config", "note": "line 12"},
        {"path": "3", "note": ""},
    ]
    assert result["finding_id"] == "risk.secrets[0]"
    assert result["source_job"] == "assess-1"
    assert result["cost_usd"] == 0.0
    (call,) = runner.calls
    # a fresh SKEPTICAL agent: security-engineer discipline, read-only tools
    assert "application security engineer" in call["system_prompt"]
    assert tuple(call["allowed_tools"]) == ("Read", "Grep", "Glob")
    assert call["cwd"] is None  # in-memory workspace has no real root
    assert "connection string committed" in call["prompt"]
    assert "Web.config" in call["prompt"]
    assert "REFUTE" in call["prompt"]
    assert "<finding-claim>" in call["prompt"]  # untrusted claim is delimited


def test_verify_finding_refuted_and_cwd_from_local_workspace(tmp_path):
    from dev_team.assessment import verify_finding

    runner = _security_verdict(
        {"verdict": "refuted", "rationale": "no such file", "citations": []}
    )
    workspace = LocalWorkspace(str(tmp_path))
    finding = _finding_fixture(evidence="")  # no citation from the auditor
    result = run(verify_finding(runner, workspace, finding))
    assert result["success"] is True
    assert result["verdict"] == "refuted"
    assert result["source_job"] is None
    (call,) = runner.calls
    assert call["cwd"] == str(workspace.root)
    assert "(none cited)" in call["prompt"]


def test_verify_finding_invalid_verdict_degrades_to_needs_context():
    from dev_team.assessment import verify_finding

    runner = _security_verdict(
        {"verdict": "definitely!", "rationale": "trust me", "citations": []}
    )
    result = run(verify_finding(runner, InMemoryWorkspace(), _finding_fixture()))
    assert result["success"] is True
    assert result["verdict"] == "needs-context"  # never promoted to a verdict
    assert "unrecognised verdict 'definitely!'" in result["rationale"]
    assert "trust me" in result["rationale"]


def test_verify_finding_missing_verdict_key_degrades_to_needs_context():
    from dev_team.assessment import verify_finding

    runner = _security_verdict({"rationale": "", "citations": []})
    result = run(verify_finding(runner, InMemoryWorkspace(), _finding_fixture()))
    assert result["verdict"] == "needs-context"
    assert "unrecognised verdict None" in result["rationale"]


def test_verify_finding_budget_exhaustion_is_a_structured_failure():
    from dev_team.assessment import verify_finding

    runner = _security_verdict({"verdict": "confirmed"})
    result = run(
        verify_finding(
            runner,
            InMemoryWorkspace(),
            _finding_fixture(),
            budget=Budget(limit_usd=0.0),
            source_job="assess-9",
        )
    )
    assert result["success"] is False
    assert result["error"] == "budget exhausted"
    assert result["cost_usd"] == 0.0
    assert result["finding_id"] == "risk.secrets[0]"
    assert result["source_job"] == "assess-9"


def test_verify_finding_unusable_response_is_a_structured_failure():
    from dev_team.assessment import verify_finding

    runner = ScriptedRunner(
        by_system_prompt={"application security engineer": "not json at all"}
    )
    result = run(verify_finding(runner, InMemoryWorkspace(), _finding_fixture()))
    assert result["success"] is False
    assert "unusable response" in result["error"]
    assert result["cost_usd"] == 0.0


def test_verify_finding_records_a_trace_span():
    from dev_team.assessment import verify_finding
    from dev_team.trace import Tracer

    tracer = Tracer(clock=lambda: 1.0)
    runner = _security_verdict({"verdict": "confirmed", "rationale": "ok"})
    run(
        verify_finding(
            runner, InMemoryWorkspace(), _finding_fixture(), tracer=tracer
        )
    )
    assert [s.name for s in tracer.by_kind("agent")] == ["verifier"]
