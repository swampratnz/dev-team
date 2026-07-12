"""Tests for the command-line interface."""

from __future__ import annotations

import json

import pytest

from helpers import happy_responses, json_response, plan_dict, design_dict, impl_dict
from helpers import review_dict, deploy_dict

from dev_team import __version__
from dev_team.cli import build_parser, main
from dev_team.testing import ScriptedRunner


def test_build_parser_parses_constraints():
    parser = build_parser()
    args = parser.parse_args(["Title", "Desc", "-c", "one", "-c", "two"])
    assert args.title == "Title"
    assert args.constraints == ["one", "two"]


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    out = capsys.readouterr().out
    assert excinfo.value.code == 0
    assert f"dev-team {__version__}" in out


def test_main_text_output_success(capsys):
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login"], runner=runner)
    out = capsys.readouterr().out
    assert code == 0
    assert "SUCCESS" in out


def test_main_json_output(capsys):
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--json"], runner=runner)
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["success"] is True


def test_main_verbose_prints_events_to_stderr(capsys):
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--verbose"], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "[workflow/" in captured.err
    assert "[workflow/" not in captured.out


def test_main_json_verbose_keeps_stdout_parseable(capsys):
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--json", "--verbose"], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert "[workflow/" in captured.err


def test_main_failure_exit_code(capsys):
    responses = [json_response(plan_dict(1)), json_response(design_dict())]
    for _ in range(2):
        responses.append(json_response(impl_dict()))
        responses.append(json_response(review_dict(False)))
    responses.append(json_response(deploy_dict()))
    runner = ScriptedRunner(responses)
    code = main(["Login", "Add login", "--max-attempts", "2"], runner=runner)
    out = capsys.readouterr().out
    assert code == 1
    assert "INCOMPLETE" in out


def test_main_invalid_config_returns_error_on_stderr(capsys):
    code = main(["Login", "Add login", "--max-attempts", "0"], runner=ScriptedRunner([]))
    captured = capsys.readouterr()
    assert code == 2
    assert "error:" in captured.err
    assert captured.out == ""


def test_main_deliver_only_flag_without_deliver_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["Login", "Add login", "--budget-usd", "5"], runner=ScriptedRunner([]))
    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    assert "--budget-usd" in err
    assert "--deliver" in err


def test_main_deliver_only_flags_all_reported(capsys):
    argv = [
        "Login", "Add login",
        "--workspace", "elsewhere",
        "--verify-command", "pytest",
        "--setup-command", "pip install -e .",
        "--branch", "custom",
        "--allow-dirty-baseline",
        "--proceed-on-red-baseline",
        "--budget-usd", "5",
        "--max-concurrency", "2",
        "--no-commit",
    ]
    with pytest.raises(SystemExit) as excinfo:
        main(argv, runner=ScriptedRunner([]))
    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    for flag in (
        "--workspace", "--verify-command", "--setup-command", "--branch",
        "--allow-dirty-baseline", "--proceed-on-red-baseline",
        "--budget-usd", "--max-concurrency", "--no-commit",
    ):
        assert flag in err


# --- real delivery mode -----------------------------------------------------


def _deliver_args(tmp_path, *extra):
    return [
        "Health endpoint",
        "Add a /health endpoint",
        "--deliver",
        "--workspace",
        str(tmp_path),
        "--no-commit",
        "--verify-command",
        "python -c pass",
        *extra,
    ]


def test_main_deliver_text_output(tmp_path, capsys):
    from helpers import engine_responses

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(_deliver_args(tmp_path), runner=runner)
    out = capsys.readouterr().out
    assert code == 0
    assert "SUCCESS" in out
    # QA's authored test file was really written into the workspace
    assert (tmp_path / "tests" / "test_x.py").exists()


def test_main_deliver_json_output(tmp_path, capsys):
    from helpers import engine_responses

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(_deliver_args(tmp_path, "--json"), runner=runner)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["success"] is True
    assert payload["committed"] is False
    assert "tests/test_x.py" in payload["workspace_files"]


def test_main_deliver_failure_exit_code(tmp_path, capsys):
    from helpers import engine_responses

    runner = ScriptedRunner(by_system_prompt=engine_responses(review=False))
    code = main(_deliver_args(tmp_path, "--max-attempts", "1"), runner=runner)
    out = capsys.readouterr().out
    assert code == 1
    assert "INCOMPLETE" in out


def test_main_deliver_passes_new_flags(tmp_path):
    from helpers import engine_responses

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(
        _deliver_args(
            tmp_path,
            "--branch", "custom/branch",
            "--allow-dirty-baseline",
            "--proceed-on-red-baseline",
            "--setup-command", "python -c pass",
        ),
        runner=runner,
    )
    assert code == 0


def test_main_deliver_auto_detects_verify_command(tmp_path, capsys):
    from helpers import engine_responses

    # a package.json makes auto-detection pick npm; npm test will fail here,
    # which surfaces as a red-baseline halt -> exit code 1 with the reason
    (tmp_path / "package.json").write_text("{}")
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    args = [
        "F", "d", "--deliver", "--workspace", str(tmp_path),
        "--no-commit", "--allow-dirty-baseline",
    ]
    code = main(args, runner=runner)
    out = capsys.readouterr().out
    assert code == 1
    assert "Halted:" in out


# --- credential preflight -----------------------------------------------------


def _no_credentials(monkeypatch, tmp_path):
    """Clear every credential source: env vars and the stored-login file."""
    from dev_team.cli import CREDENTIAL_ENV_VARS

    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.claude/.credentials.json


@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ],
)
def test_ensure_credentials_accepts_each_env_var(name, tmp_path):
    from dev_team.cli import ensure_credentials

    ensure_credentials(environ={name: "value"}, home=tmp_path)


def test_ensure_credentials_ignores_empty_env_var(tmp_path):
    from dev_team.cli import ensure_credentials
    from dev_team.errors import DevTeamError

    with pytest.raises(DevTeamError):
        ensure_credentials(environ={"ANTHROPIC_API_KEY": ""}, home=tmp_path)


def test_ensure_credentials_accepts_stored_login(tmp_path):
    from dev_team.cli import ensure_credentials

    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir()
    creds.write_text("{}")
    ensure_credentials(environ={}, home=tmp_path)


def test_ensure_credentials_error_mentions_setup_token(tmp_path):
    from dev_team.cli import ensure_credentials
    from dev_team.errors import DevTeamError

    with pytest.raises(DevTeamError) as excinfo:
        ensure_credentials(environ={}, home=tmp_path)
    message = str(excinfo.value)
    assert "claude setup-token" in message
    assert "CLAUDE_CODE_OAUTH_TOKEN" in message
    assert "ANTHROPIC_API_KEY" in message


def test_main_without_credentials_fails_fast(monkeypatch, tmp_path, capsys):
    _no_credentials(monkeypatch, tmp_path)
    code = main(["Login", "Add login"])  # no injected runner -> real SDK path
    captured = capsys.readouterr()
    assert code == 2
    assert "claude setup-token" in captured.err
    assert captured.out == ""


def test_main_with_injected_runner_skips_credential_check(monkeypatch, tmp_path, capsys):
    _no_credentials(monkeypatch, tmp_path)
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login"], runner=runner)
    assert code == 0
    assert "SUCCESS" in capsys.readouterr().out


# --- personas, interactivity, chat -------------------------------------------


def test_main_rejects_chat_with_positionals(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["Login", "Add login", "--chat"], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "--chat" in capsys.readouterr().err


def test_main_rejects_chat_with_json(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--chat", "--json"], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "--json" in capsys.readouterr().err


def test_main_requires_positionals_without_chat(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "required" in capsys.readouterr().err


def test_main_rejects_roster_with_no_personas(tmp_path, capsys):
    roster = tmp_path / "roster.json"
    roster.write_text("{}")
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["Login", "Add login", "--roster", str(roster), "--no-personas"],
            runner=ScriptedRunner([]),
        )
    assert excinfo.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_verbose_events_use_persona_names(capsys):
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--verbose"], runner=runner)
    assert code == 0
    assert "Priya (product-manager)" in capsys.readouterr().err


def test_main_no_personas_uses_bare_roles(capsys):
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--verbose", "--no-personas"], runner=runner)
    err = capsys.readouterr().err
    assert code == 0
    assert "[product-manager/planning]" in err
    assert "Priya" not in err


def test_main_roster_file_renames_agents(tmp_path, capsys):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"product-manager": {"name": "Petra"}}))
    runner = ScriptedRunner(happy_responses(1))
    code = main(
        ["Login", "Add login", "--verbose", "--roster", str(roster)], runner=runner
    )
    assert code == 0
    assert "Petra (product-manager)" in capsys.readouterr().err


def test_main_bad_roster_file_exits_2(tmp_path, capsys):
    roster = tmp_path / "roster.json"
    roster.write_text("{broken")
    code = main(
        ["Login", "Add login", "--roster", str(roster)], runner=ScriptedRunner([])
    )
    assert code == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_main_interactive_simulation_reads_stdin(monkeypatch, capsys):
    import io as _io

    monkeypatch.setattr("sys.stdin", _io.StringIO("approve\n"))
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--interactive"], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "SUCCESS" in captured.out
    assert "Approve this plan" in captured.err


def test_main_interactive_abort_exits_2(monkeypatch, capsys):
    import io as _io

    monkeypatch.setattr("sys.stdin", _io.StringIO("abort\n"))
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--interactive"], runner=runner)
    captured = capsys.readouterr()
    assert code == 2
    assert "aborted at plan review" in captured.err


def test_main_interactive_deliver_wires_approval_gate(monkeypatch, tmp_path, capsys):
    import io as _io

    from helpers import engine_responses

    monkeypatch.setattr("sys.stdin", _io.StringIO("approve\n"))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(_deliver_args(tmp_path, "--interactive"), runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "SUCCESS" in captured.out
    assert "Approve this plan" in captured.err


def test_main_chat_runs_simulation_from_conversation(monkeypatch, capsys):
    import io as _io

    from test_chat import FakeBackend

    monkeypatch.setattr("sys.stdin", _io.StringIO("I want login\n/run\n/quit\n"))
    backend = FakeBackend(["what kind of login?"])
    runner = ScriptedRunner(happy_responses(1))
    code = main(["--chat"], runner=runner, chat_backend=backend)
    captured = capsys.readouterr()
    assert code == 0
    assert "chatting with Priya" in captured.out
    assert "Priya > what kind of login?" in captured.out
    assert "SUCCESS" in captured.out
    assert backend.closed is True


def test_main_chat_deliver_from_conversation(monkeypatch, tmp_path, capsys):
    import io as _io

    from helpers import engine_responses
    from test_chat import FakeBackend

    monkeypatch.setattr("sys.stdin", _io.StringIO("/deliver\n/quit\n"))
    backend = FakeBackend()
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(
        ["--chat", "--workspace", str(tmp_path), "--no-commit",
         "--verify-command", "python -c pass"],
        runner=runner,
        chat_backend=backend,
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "handing off to the team (delivery)" in captured.out
    assert "SUCCESS" in captured.out


def test_main_chat_without_personas_uses_role_name(monkeypatch, capsys):
    import io as _io

    from test_chat import FakeBackend

    monkeypatch.setattr("sys.stdin", _io.StringIO("/quit\n"))
    code = main(
        ["--chat", "--no-personas"],
        runner=ScriptedRunner([]),
        chat_backend=FakeBackend(),
    )
    assert code == 0
    assert "chatting with product-manager" in capsys.readouterr().out


def test_main_chat_builds_real_backend_lazily(monkeypatch, capsys):
    import io as _io

    # No injected backend: the real ClaudeChatBackend is constructed but its
    # session never starts because the user quits before saying anything.
    monkeypatch.setattr("sys.stdin", _io.StringIO("/quit\n"))
    code = main(["--chat"], runner=ScriptedRunner([]))
    assert code == 0
    assert "chatting with Priya" in capsys.readouterr().out


# --- assess mode ----------------------------------------------------------------


def _dotnet_repo(tmp_path):
    (tmp_path / "MyApp.sln").write_text("Microsoft Visual Studio Solution File")
    src = tmp_path / "src" / "Api"
    src.mkdir(parents=True)
    (src / "Api.csproj").write_text("<Project/>")
    return tmp_path


def test_main_assess_writes_report_and_prints_markdown(tmp_path, capsys):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--workspace", str(repo)], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "# Repository assessment" in captured.out
    assert "**Classification: dependency-surgery**" in captured.out
    assert (repo / "audit" / "assessment.md").exists()


def test_main_assess_json_output(tmp_path, capsys):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--workspace", str(repo), "--json"], runner=runner)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["classification"] == "dependency-surgery"
    assert payload["profile"]["kind"] == "dotnet"


def test_main_assess_custom_report_path_and_focus(tmp_path, capsys):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(
        [
            "--assess", "--workspace", str(repo),
            "--report", "audit/2026-07-12_01_legacy-assessment.md",
            "Legacy monolith", "dormant 2-3 years, frontend + backend",
        ],
        runner=runner,
    )
    assert code == 0
    assert (repo / "audit" / "2026-07-12_01_legacy-assessment.md").exists()
    focus_calls = [c for c in runner.calls if "dormant 2-3 years" in c["prompt"]]
    assert focus_calls, "the description scoped the audit prompts"
    out = capsys.readouterr().out
    assert "Legacy monolith — dormant 2-3 years" in out


def test_main_assess_failure_exit_code(tmp_path, capsys):
    from test_assessment import assess_responses, recommendation_dict

    repo = _dotnet_repo(tmp_path)
    responses = assess_responses(
        **{"product manager": recommendation_dict(classification="nonsense")}
    )
    runner = ScriptedRunner(by_system_prompt=responses)
    code = main(["--assess", "--workspace", str(repo)], runner=runner)
    assert code == 1


def test_main_assess_interactive_scope_prompt(monkeypatch, tmp_path, capsys):
    import io as _io

    from test_assessment import assess_responses

    monkeypatch.setattr("sys.stdin", _io.StringIO("continue\n"))
    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--workspace", str(repo), "--interactive"], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "Adjust the audit scope" in captured.err


def test_main_assess_rejects_chat_and_deliver(capsys):
    for combo in (["--assess", "--chat"], ["--assess", "--deliver", "T", "D"]):
        with pytest.raises(SystemExit) as excinfo:
            main(combo, runner=ScriptedRunner([]))
        assert excinfo.value.code == 2


def test_main_report_requires_assess(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["T", "D", "--report", "x.md"], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "--report" in capsys.readouterr().err


def test_main_assess_rejects_deliver_only_flags(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--assess", "--branch", "x"], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "--branch" in capsys.readouterr().err


def test_main_assess_allows_workspace_and_budget(tmp_path):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(
        ["--assess", "--workspace", str(repo), "--budget-usd", "10"], runner=runner
    )
    assert code == 0
