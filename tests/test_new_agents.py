"""Tests for the security, technical writer, and SRE agents."""

from __future__ import annotations

from helpers import run

from dev_team.agents import SecurityEngineerAgent, SREAgent, TechnicalWriterAgent
from dev_team.models import (
    ChangeType,
    Design,
    Documentation,
    FeatureRequest,
    FileChange,
    Implementation,
    ReliabilityReport,
    SecurityReport,
    Severity,
    Task,
)
from dev_team.testing import ScriptedRunner, json_response


def _runner(payload):
    return ScriptedRunner([json_response(payload)])


def _task():
    return Task(id="T1", title="Build", description="d")


def test_security_agent_with_files():
    payload = {
        "approved": False,
        "summary": "vuln",
        "findings": [
            {"severity": "critical", "category": "xss", "description": "d", "remediation": "escape"}
        ],
    }
    agent = SecurityEngineerAgent(_runner(payload))
    impl = Implementation(
        task_id="T1",
        summary="s",
        files=[FileChange("a.py", ChangeType.CREATE, "adds")],
    )
    report = run(agent.review(_task(), impl))
    assert isinstance(report, SecurityReport)
    assert report.approved is False
    assert report.blocking_findings[0].severity is Severity.CRITICAL


def test_security_agent_without_files():
    runner = _runner({"approved": True, "summary": "ok", "findings": []})
    agent = SecurityEngineerAgent(runner)
    impl = Implementation(task_id="T1", summary="s", files=[])
    report = run(agent.review(_task(), impl))
    assert report.approved is True
    assert "(no files reported)" in runner.calls[0]["prompt"]


def test_security_agent_read_only_tools_and_fenced_scanner_output():
    runner = _runner({"approved": True, "summary": "ok", "findings": []})
    agent = SecurityEngineerAgent(runner)
    impl = Implementation(task_id="T1", summary="s", files=[])
    run(
        agent.review(
            _task(),
            impl,
            scanner_output="bandit: eval() used",
            workspace_root="/ws",
        )
    )
    call = runner.calls[0]
    assert tuple(call["allowed_tools"]) == ("Read", "Grep", "Glob")
    assert call["cwd"] == "/ws"
    assert "<scanner-output>\nbandit: eval() used\n</scanner-output>" in call["prompt"]
    assert "untrusted data under review" in call["system_prompt"]


def test_technical_writer_produces_doc_files():
    payload = {
        "summary": "docs",
        "sections": [{"title": "Overview", "content": "..."}],
        "files": [
            {
                "path": "docs/feature.md",
                "change_type": "create",
                "summary": "user docs",
                "content": "# Feature\nUsage...",
            }
        ],
    }
    runner = _runner(payload)
    agent = TechnicalWriterAgent(runner)
    impl = Implementation(
        task_id="T1",
        summary="s",
        files=[FileChange("a.py", ChangeType.CREATE, "adds")],
    )
    docs, doc_files = run(
        agent.write_docs(
            FeatureRequest(title="F", description="d"),
            Design(overview="o"),
            impl,
            file_contents={"a.py": "GROUNDING_CONTENT"},
            existing_docs=["README.md"],
        )
    )
    assert isinstance(docs, Documentation)
    assert docs.sections[0].title == "Overview"
    assert doc_files.files[0].path == "docs/feature.md"
    prompt = runner.calls[0]["prompt"]
    assert "GROUNDING_CONTENT" in prompt  # grounded in real code
    assert "README.md" in prompt  # aware of existing docs


def test_technical_writer_without_docs_or_files():
    payload = {"summary": "docs", "sections": []}
    runner = _runner(payload)
    agent = TechnicalWriterAgent(runner)
    impl = Implementation(task_id="T1", summary="s", files=[])
    docs, doc_files = run(
        agent.write_docs(
            FeatureRequest(title="F", description="d"), Design(overview="o"), impl
        )
    )
    assert doc_files.files == []
    assert "- (none)" in runner.calls[0]["prompt"]


def test_sre_agent_with_stack():
    payload = {
        "production_ready": True,
        "summary": "ready",
        "slos": ["99.9%"],
        "risks": [],
        "runbook": ["restart"],
    }
    agent = SREAgent(_runner(payload))
    report = run(
        agent.assess(
            FeatureRequest(title="F", description="d"),
            Design(overview="o", tech_stack=["python"]),
        )
    )
    assert isinstance(report, ReliabilityReport)
    assert report.production_ready is True


def test_sre_agent_without_stack():
    runner = _runner(
        {"production_ready": False, "summary": "no", "slos": [], "risks": ["x"], "runbook": []}
    )
    agent = SREAgent(runner)
    run(agent.assess(FeatureRequest(title="F", description="d"), Design(overview="o")))
    assert "unspecified" in runner.calls[0]["prompt"]


def test_sre_agent_read_only_tools_and_workspace_root():
    runner = _runner(
        {"production_ready": True, "summary": "ok", "slos": [], "risks": [], "runbook": []}
    )
    agent = SREAgent(runner)
    run(
        agent.assess(
            FeatureRequest(title="F", description="d"),
            Design(overview="o"),
            workspace_root="/ws",
        )
    )
    call = runner.calls[0]
    assert tuple(call["allowed_tools"]) == ("Read", "Grep", "Glob")
    assert call["cwd"] == "/ws"
