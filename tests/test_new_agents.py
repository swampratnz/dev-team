"""Tests for the security, technical writer, and SRE agents."""

from __future__ import annotations

import pytest

from helpers import run

from dev_team.agents import SecurityEngineerAgent, SREAgent, TechnicalWriterAgent
from dev_team.agents.techwriter import doc_claim_issues
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


# --- doc_claim_issues ----------------------------------------------------


def _doc(content, path="docs/feature.md"):
    return FileChange(path, ChangeType.CREATE, "docs", content)


def test_doc_claim_issues_no_findings_for_no_doc_files():
    assert doc_claim_issues([], ["src/real.py"]) == []


def test_doc_claim_issues_no_finding_when_cited_path_exists():
    doc = _doc("See `src/real.py` for the implementation.")
    assert doc_claim_issues([doc], ["src/real.py"]) == []


def test_doc_claim_issues_flags_a_path_absent_from_known_files():
    doc = _doc("See `src/missing.py` for the implementation.")
    issues = doc_claim_issues([doc], ["src/real.py"])
    assert len(issues) == 1
    assert "docs/feature.md" in issues[0]
    assert "src/missing.py" in issues[0]


def test_doc_claim_issues_valid_python_fence_has_no_finding():
    doc = _doc("```python\ndef add(a, b):\n    return a + b\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_does_not_scan_fenced_code_for_path_citations():
    # A dotted attribute reference inside a code fence (os.system) must not
    # be mistaken for a broken file citation — only prose text outside
    # fences is scanned for citations.
    doc = _doc("See `src/real.py` for details.\n\n```python\nos.system('id')\n```\n")
    assert doc_claim_issues([doc], ["src/real.py"]) == []


def test_doc_claim_issues_strips_trailing_sentence_punctuation():
    # Neither an ordinary prose word ending a sentence, nor a genuine path
    # citation that happens to end one, should misfire.
    doc = _doc("Read the summary for details. Also see `docs/guide.md`.")
    assert doc_claim_issues([doc], ["docs/guide.md"]) == []


def test_doc_claim_issues_flags_a_broken_python_fence_with_line_number():
    doc = _doc("```python\ndef add(a, b)\n    return a + b\n```\n")
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1
    assert "docs/feature.md" in issues[0]
    assert "line" in issues[0]


def test_doc_claim_issues_ignores_non_python_fences():
    doc = _doc("```bash\ndef broken(:::\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_never_executes_fence_content():
    import subprocess
    from unittest.mock import patch

    doc = _doc(
        '```python\nimport os, subprocess\n'
        'os.system("id")\n'
        'subprocess.run(["rm", "-rf", "/"])\n'
        "```\n"
    )
    with patch("os.system") as mock_system, patch.object(
        subprocess, "run"
    ) as mock_run:
        issues = doc_claim_issues([doc], [])
    assert issues == []
    mock_system.assert_not_called()
    mock_run.assert_not_called()


def test_doc_claim_issues_traversal_and_absolute_paths_are_unresolved_by_set_membership():
    # known_files deliberately excludes both strings; doc_claim_issues must
    # only ever compare via set membership against known_files, never touch
    # a real filesystem (this module imports no os/pathlib at all) — a run
    # against these traversal-shaped strings must not raise, just flag them.
    doc = _doc("See ../../../etc/passwd and /etc/passwd for details.")
    issues = doc_claim_issues([doc], ["src/real.py"])
    assert len(issues) == 2
    assert any("../../../etc/passwd" in i for i in issues)
    assert any("/etc/passwd" in i for i in issues)


def test_doc_claim_issues_handles_unterminated_fence_without_raising():
    doc = _doc("```python\ndef broken(:\nno closing fence here")
    assert doc_claim_issues([doc], []) == []


# --- doc_claim_issues: CLI-flag checking in shell fences ------------------


def test_doc_claim_issues_flags_hallucinated_cli_flag():
    doc = _doc("```bash\ndev-team --made-up-flag\n```\n")
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1
    assert "docs/feature.md" in issues[0]
    assert "--made-up-flag" in issues[0]


def test_doc_claim_issues_no_finding_for_real_cli_flags():
    doc = _doc("```bash\ndev-team --deliver --workspace .\n```\n")
    assert doc_claim_issues([doc], []) == []


@pytest.mark.parametrize("lang", ["bash", "sh", "shell", "console", "zsh"])
def test_doc_claim_issues_checks_all_shell_fence_languages(lang):
    doc = _doc(f"```{lang}\ndev-team --made-up-flag\n```\n")
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1


@pytest.mark.parametrize("lang", ["bash", "sh", "shell", "console", "zsh"])
def test_doc_claim_issues_checks_shell_fence_with_prompt_marker(lang):
    doc = _doc(f"```{lang}\n$ dev-team --made-up-flag\n```\n")
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1


def test_doc_claim_issues_recognises_python_module_invocation():
    doc = _doc("```bash\npython -m dev_team --made-up-flag\n```\n")
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1


def test_doc_claim_issues_recognises_python3_module_invocation():
    doc = _doc("```bash\npython3 -m dev_team --made-up-flag\n```\n")
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1


def test_doc_claim_issues_ignores_non_cli_invocation_shell_lines():
    doc = _doc("```bash\ncurl -X POST https://example.com --data '{}'\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_truncates_at_unquoted_pipe():
    # Without truncation, "--raw-output" (a jq flag, not a dev-team one)
    # would be misread as a cited dev-team flag.
    doc = _doc("```bash\ndev-team --deliver | jq --raw-output .x\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_truncates_at_unquoted_double_ampersand():
    # Without truncation, "--raw-output" (an unrelated flag) would be
    # misread as a cited dev-team flag.
    doc = _doc("```bash\ndev-team --deliver && jq --raw-output .x\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_does_not_truncate_at_quoted_delimiter():
    # The ';' is inside a quoted value, so it must not truncate the line
    # before the real, unrecognised flag that follows it.
    doc = _doc('```bash\ndev-team --deliver "a; b" --typo-flag\n```\n')
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1
    assert "--typo-flag" in issues[0]


def test_doc_claim_issues_ignores_short_flags():
    doc = _doc("```bash\ndev-team -x\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_equals_form_real_flag_has_no_finding():
    doc = _doc("```bash\ndev-team --budget-usd=5\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_equals_form_misspelled_flag_flags():
    doc = _doc("```bash\ndev-team --budget-us=5\n```\n")
    issues = doc_claim_issues([doc], [])
    assert len(issues) == 1
    assert "--budget-us" in issues[0]


def test_doc_claim_issues_shell_fence_never_shells_out_or_executes():
    import os
    import subprocess
    from unittest.mock import patch

    doc = _doc("```bash\ndev-team --bogus-flag; rm -rf /\n```\n")
    with patch.object(
        subprocess, "run", side_effect=AssertionError("must not run")
    ) as mock_run, patch.object(
        os, "system", side_effect=AssertionError("must not run")
    ) as mock_system, patch.object(
        os, "popen", side_effect=AssertionError("must not run")
    ) as mock_popen:
        issues = doc_claim_issues([doc], [])
    mock_run.assert_not_called()
    mock_system.assert_not_called()
    mock_popen.assert_not_called()
    assert len(issues) == 1
    assert "--bogus-flag" in issues[0]


def test_doc_claim_issues_reads_known_flags_from_the_live_parser(monkeypatch):
    import dev_team.cli as cli_module

    real_build_parser = cli_module.build_parser

    def build_parser_with_temp_flag():
        parser = real_build_parser()
        parser.add_argument("--totally-new-flag", action="store_true")
        return parser

    monkeypatch.setattr(cli_module, "build_parser", build_parser_with_temp_flag)
    doc = _doc("```bash\ndev-team --totally-new-flag\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_doc_claim_issues_other_fence_languages_unaffected_by_cli_check():
    doc = _doc("```json\n{\"--not-a-flag\": true}\n```\n")
    assert doc_claim_issues([doc], []) == []


def test_techwriter_import_does_not_trigger_cli_import():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import dev_team.agents.techwriter; "
            "assert 'dev_team.cli' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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
