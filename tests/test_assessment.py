"""Tests for the read-only assessment engine."""

from __future__ import annotations


from helpers import run

from dev_team.assessment import (
    AssessConfig,
    AssessmentEngine,
    InventoryStats,
    PhaseResult,
    inventory_stats,
    outcome_to_dict,
    scope_question,
)
from dev_team.budget import Budget
from dev_team.execution import InMemoryWorkspace, LocalWorkspace
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


def assess_responses(**overrides):
    """Keyed responses for every auditing role (override per test)."""

    payloads = {
        "software architect": inventory_dict(),
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
    assert after - before == {"audit/assessment.md"}  # no .dev_team/, no edits


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


def test_cited_handles_items_without_fields():
    from dev_team.assessment import _cited

    lines = []
    _cited(lines, [{"evidence": "a.cs"}, {}], "claim")
    assert lines == ["- (unspecified) (evidence: a.cs)", "- (unspecified)"]
