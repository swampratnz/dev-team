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


def test_min_coverage_default_is_pragmatic():
    # F3: 100 is strict and marks most first runs INCOMPLETE; default to 80.
    parser = build_parser()
    assert parser.parse_args(["Title", "Desc"]).min_coverage == 80.0


def test_build_parser_groups_flags_for_help():
    # U4: flags are organised into argument groups for --help readability;
    # grouping is cosmetic and must not change parsing.
    parser = build_parser()
    titles = [g.title for g in parser._action_groups]
    for expected in (
        "modes",
        "delivery options",
        "assessment options",
        "serving options",
    ):
        assert expected in titles


def test_workspace_help_names_deliver_assess_dashboard():
    # U4: --workspace is the target dir for --deliver, --assess and --dashboard,
    # not just --deliver as the old help implied.
    parser = build_parser()
    workspace = next(a for a in parser._actions if a.dest == "workspace")
    assert "--deliver" in workspace.help
    assert "--assess" in workspace.help
    assert "--dashboard" in workspace.help


def test_build_parser_epilog_documents_exit_codes():
    # U13.2: exit-code semantics are surfaced in --help, not only the README.
    epilog = build_parser().epilog
    assert "exit codes" in epilog
    assert "success" in epilog
    assert "invalid input" in epilog
    assert "130" in epilog
    assert "interrupted" in epilog


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
    # --budget-usd is no longer deliver-only (the simulation meters cost too);
    # --no-commit still is, so it is what the rejection should name here.
    with pytest.raises(SystemExit) as excinfo:
        main(["Login", "Add login", "--no-commit"], runner=ScriptedRunner([]))
    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    assert "--no-commit" in err
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
        "--max-concurrency", "--no-commit",
    ):
        assert flag in err


def test_main_simulation_accepts_budget_and_reports_cost(monkeypatch, capsys):
    # F1: the simulation is not free — --budget-usd is now accepted, threaded
    # into the workflow per the G contract, and the metered cost is reported.
    import dev_team.cli as cli_module
    from dev_team.models import Design, Plan, ProjectResult

    captured = {}

    class _FakeTeam:
        def __init__(self, *a, **k):
            self.interaction = None

        async def develop(self, request, *, budget=None):
            captured["budget"] = budget
            result = ProjectResult(
                request=request,
                plan=Plan(summary="p"),
                design=Design(overview="o"),
                task_results=[],
            )
            result.cost_usd = 0.1234  # G's backend populates this from the meter
            return result

    monkeypatch.setattr(cli_module, "DevTeam", _FakeTeam)
    code = main(["Login", "Add login", "--budget-usd", "5"], runner=ScriptedRunner([]))
    out = capsys.readouterr().out
    assert code == 1  # no tasks succeeded, but the run was accepted (not exit 2)
    assert captured["budget"].limit_usd == 5.0
    assert "Cost:    $0.1234" in out


def test_main_simulation_prints_paid_agent_notice(capsys):
    # U5: the default (simulation) mode drives the same paid agents, so it says
    # so once up front on stderr (stdout stays a clean result document).
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login"], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "paid agents" in captured.err
    assert captured.err.count("paid agents") == 1  # once, not per event
    assert "paid agents" not in captured.out


def test_main_simulation_notice_suppressed_by_quiet(capsys):
    # U5: --quiet silences the upfront notice.
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--quiet"], runner=runner)
    captured = capsys.readouterr()
    assert code == 0
    assert "paid agents" not in captured.err


def test_main_keyboard_interrupt_returns_130(capsys):
    # U8: Ctrl-C during a run exits 130 with a clean line, no asyncio traceback.
    class _InterruptingRunner:
        async def run(self, prompt, *, system_prompt=None, allowed_tools=None,
                      model=None, cwd=None):
            raise KeyboardInterrupt

    code = main(["Login", "Add login"], runner=_InterruptingRunner())
    captured = capsys.readouterr()
    assert code == 130
    assert "interrupted" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_main_min_coverage_rejected_with_deliver(capsys):
    # F2: --min-coverage is a simulation-mode gate; the delivery engine ignores
    # it, so passing both is an error rather than a silent no-op.
    with pytest.raises(SystemExit) as excinfo:
        main(["T", "D", "--deliver", "--min-coverage", "90"], runner=ScriptedRunner([]))
    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    assert "--min-coverage" in err
    assert "definition-of-done" in err


def test_main_budget_usd_rejected_in_unmetered_modes(tmp_path, capsys):
    # F1 corollary: modes that run no metered agents have no cost ceiling to
    # enforce, so --budget-usd there is a user error, not a silent no-op.
    for extra in (
        ["--dashboard", "--workspace", str(tmp_path)],
        ["--make-backlog", str(tmp_path)],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main([*extra, "--budget-usd", "5"], runner=ScriptedRunner([]))
        assert excinfo.value.code == 2
        assert "--budget-usd" in capsys.readouterr().err


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


def test_main_deliver_default_progress_and_quiet_suppresses(tmp_path, capsys):
    # F6: --deliver shows a lightweight running-cost progress line on stderr by
    # default; --quiet silences it. (-v prints the full stream instead — the
    # two are mutually exclusive, so events are never double-printed.)
    from helpers import engine_responses

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    assert main(_deliver_args(tmp_path), runner=runner) == 0
    err = capsys.readouterr().err
    assert "$0.0000" in err  # the running cost went to stderr

    runner2 = ScriptedRunner(by_system_prompt=engine_responses())
    assert main(_deliver_args(tmp_path, "--quiet"), runner=runner2) == 0
    assert "$" not in capsys.readouterr().err  # --quiet suppresses the progress


# --- credential preflight -----------------------------------------------------


def _no_credentials(monkeypatch, tmp_path):
    """Clear every credential source: env vars and the stored-login file."""
    from dev_team.cli import CREDENTIAL_ENV_VARS

    for name in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.claude/.credentials.json
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows spells HOME this way


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

    monkeypatch.setattr("sys.stdin", _io.StringIO("I want login\n/run\ny\n/quit\n"))
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

    monkeypatch.setattr("sys.stdin", _io.StringIO("/deliver\ny\n/quit\n"))
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


def test_main_assess_missing_workspace_exits_2(tmp_path, capsys):
    # U2: --assess audits an existing repo; a mistyped --workspace must fail
    # loudly rather than silently audit an empty dir LocalWorkspace mkdirs.
    missing = tmp_path / "nope"
    with pytest.raises(SystemExit) as excinfo:
        main(["--assess", "--workspace", str(missing)], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "--assess" in capsys.readouterr().err
    assert not missing.exists()  # the typo did not create it


def test_main_assess_empty_workspace_exits_2(tmp_path, capsys):
    # U2: an existing but empty --workspace is also a likely typo, not a repo.
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        main(["--assess", "--workspace", str(empty)], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "--assess" in capsys.readouterr().err


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


def test_main_assess_new_flags_reach_config(tmp_path, capsys):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    (repo / "junk").mkdir()
    (repo / "junk" / "vendored.cs").write_text("class V {}")
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(
        [
            "--assess", "--workspace", str(repo),
            "--exclude", "junk/*",
            "--max-tree-entries", "50",
            "--component-fanout",
            "--no-osv-scan",
            "--no-eol-scan",
            "--backlog",
            "--no-conventions",
            "--json",
        ],
        runner=runner,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "junk/vendored.cs" not in json.dumps(payload["stats"])
    assert payload["dependency_scan"]["error"] == "scan disabled"
    assert payload["eol_scan"]["error"] == "scan disabled"
    assert "components" in payload["phases"]
    assert payload["backlog_stories"]
    assert (repo / ".dev_team" / "backlog.json").exists()
    assert not (repo / ".dev_team" / "conventions.json").exists()


def test_main_assess_build_probe_reaches_config(tmp_path, capsys):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    # legacy NuGet restore ⇒ dotnet-framework profile ⇒ the probe has no
    # locally runnable commands, so the CLI path stays hermetic.
    (repo / "packages.config").write_text("<packages/>")
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(
        ["--assess", "--workspace", str(repo), "--build-probe", "--json"],
        runner=runner,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["build_probe"]["requested"] is True
    assert "no locally runnable" in payload["build_probe"]["skipped_reason"]


def test_assess_only_flags_rejected_outside_assess():
    for flag in (
        ["--exclude", "junk/*"],
        ["--max-tree-entries", "50"],
        ["--component-fanout"],
        ["--no-osv-scan"],
        ["--no-eol-scan"],
        ["--backlog"],
        ["--no-conventions"],
        ["--build-probe"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(["Login", "Add login", *flag], runner=ScriptedRunner([]))
        assert excinfo.value.code == 2


def test_remote_verify_flags_require_deliver_and_status():
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["Login", "Add login", "--remote-verify-status", "ci status"],
            runner=ScriptedRunner([]),
        )
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "Login", "Add login", "--deliver",
                "--remote-verify-trigger", "ci run",
            ],
            runner=ScriptedRunner([]),
        )
    assert excinfo.value.code == 2


def test_deliver_remote_verify_builds_remote_gate(tmp_path, monkeypatch):
    import dev_team.cli as cli_module
    from dev_team.verification import RemoteCIGate

    captured = {}

    class _FakeTeam:
        def __init__(self, *a, **k):
            self.interaction = None
            self.roster = None

        async def deliver(self, request, **kwargs):
            captured["config"] = kwargs["config"]
            raise SystemExit(0)

    monkeypatch.setattr(cli_module, "DevTeam", _FakeTeam)
    with pytest.raises(SystemExit):
        main(
            [
                "Fix", "Fix the thing", "--deliver",
                "--workspace", str(tmp_path),
                "--remote-verify-status", "az pipelines runs show --status",
                "--remote-verify-trigger", "az pipelines run",
            ],
            runner=ScriptedRunner([]),
        )
    config = captured["config"]
    assert config.remote_verify_status == ("az", "pipelines", "runs", "show", "--status")
    assert config.remote_verify_trigger == ("az", "pipelines", "run")
    from dev_team.engine import DeliveryEngine
    from dev_team.execution import FakeCommandRunner, InMemoryWorkspace

    engine = DeliveryEngine(
        ScriptedRunner([]),
        workspace=InMemoryWorkspace(),
        command_runner=FakeCommandRunner(),
        config=config,
    )
    assert isinstance(engine.definition_of_done.gates[0], RemoteCIGate)


# --- --make-backlog ---------------------------------------------------------------


def _persisted_assessment(tmp_path):
    """A workspace holding a minimal persisted assessment (one plan step)."""

    ws = tmp_path / "repo"
    (ws / ".dev_team").mkdir(parents=True)
    payload = {
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
    (ws / ".dev_team" / "assessment.json").write_text(json.dumps(payload))
    return ws


def test_main_make_backlog_generates_stories_without_credentials(
    monkeypatch, tmp_path, capsys
):
    _no_credentials(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(
        "dev_team.cli.ensure_credentials", lambda *a, **k: called.append(1)
    )
    ws = _persisted_assessment(tmp_path)
    code = main(["--make-backlog", str(ws)])  # no injected runner
    out = capsys.readouterr().out
    assert code == 0
    assert called == []  # the offline transform never checks credentials
    assert "1 story(ies) added" in out
    assert ".dev_team/backlog.json" in out
    stored = json.loads((ws / ".dev_team" / "backlog.json").read_text())
    assert [s["title"] for s in stored["stories"]] == ["Pin build chain"]
    assert stored["epics"][0]["title"] == "Assessment remediation"


def test_main_make_backlog_json_output_and_dedupe(tmp_path, capsys):
    ws = _persisted_assessment(tmp_path)
    assert main(["--make-backlog", str(ws), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "stories_added": 1,
        "stories_total": 1,
    }
    # re-running dedupes by title: free to repeat, never floods
    assert main(["--make-backlog", str(ws), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "stories_added": 0,
        "stories_total": 1,
    }


def test_main_make_backlog_uses_job_meta_for_per_repo_epic(tmp_path, capsys):
    """With meta.json beside the assessment, stories get repo epic + provenance."""

    ws = _persisted_assessment(tmp_path)
    (ws / ".dev_team" / "meta.json").write_text(
        json.dumps({"repo": "acme/rota", "mode": "assess", "id": "assess-7"})
    )
    assert main(["--make-backlog", str(ws), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "stories_added": 1,
        "stories_total": 1,
    }
    stored = json.loads((ws / ".dev_team" / "backlog.json").read_text())
    assert stored["epics"][0]["title"] == "Remediation — acme/rota"
    (story,) = stored["stories"]
    assert story["source_job"] == "assess-7"
    assert story["finding_id"] == "recommendation.plan[0]"


def test_main_make_backlog_missing_assessment_exits_2(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()  # the dir exists (so the isdir guard passes); it just has no assessment
    code = main(["--make-backlog", str(empty)])
    captured = capsys.readouterr()
    assert code == 2
    assert "no assessment.json" in captured.err
    assert "--assess" in captured.err
    assert captured.out == ""


def test_main_make_backlog_missing_directory_exits_2(tmp_path, capsys):
    # F5: a mistyped --make-backlog path fails loudly instead of mkdir-ing it.
    missing = tmp_path / "nope"
    with pytest.raises(SystemExit) as excinfo:
        main(["--make-backlog", str(missing)], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "no such directory" in capsys.readouterr().err
    assert not missing.exists()


def test_main_make_backlog_is_standalone():
    for extra in (
        ["--assess"],
        ["--deliver"],
        ["--chat"],
        ["--dashboard"],
        ["--dispatch"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(["--make-backlog", ".", *extra], runner=ScriptedRunner([]))
        assert excinfo.value.code == 2


def test_main_assess_persists_result_then_make_backlog_generates(tmp_path, capsys):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    assert main(["--assess", "--workspace", str(repo)], runner=runner) == 0
    assert (repo / ".dev_team" / "assessment.json").exists()
    capsys.readouterr()  # drop the printed report
    # later, credential-free and $0: turn the persisted audit into stories
    code = main(["--make-backlog", str(repo), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["stories_added"] > 0
    assert payload["stories_total"] == payload["stories_added"]


# --- --verify / --finding -----------------------------------------------------------


def _verify_runner(payload=None):
    from dev_team.testing import json_response

    payload = payload or {
        "verdict": "confirmed",
        "rationale": "read the file",
        "citations": [{"path": "global.json", "note": "pin present"}],
    }
    return ScriptedRunner(
        by_system_prompt={"application security engineer": json_response(payload)}
    )


def test_main_verify_text_output(tmp_path, capsys):
    ws = _persisted_assessment(tmp_path)
    code = main(
        ["--verify", str(ws), "--finding", "recommendation.plan[0]"],
        runner=_verify_runner(),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "recommendation.plan[0] — confirmed" in out
    assert "claim: Pin build chain" in out
    assert "rationale: read the file" in out
    assert "  - global.json: pin present" in out
    assert "cost: $0.0000" in out


def test_main_verify_json_output_and_claim_substring(tmp_path, capsys):
    ws = _persisted_assessment(tmp_path)
    code = main(
        ["--verify", str(ws), "--finding", "pin BUILD", "--json"],
        runner=_verify_runner(),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["success"] is True
    assert payload["verdict"] == "confirmed"
    assert payload["finding_id"] == "recommendation.plan[0]"
    assert payload["citations"] == [{"path": "global.json", "note": "pin present"}]


def test_main_verify_refuted_without_rationale_or_citations(tmp_path, capsys):
    ws = _persisted_assessment(tmp_path)
    code = main(
        ["--verify", str(ws), "--finding", "Pin build chain"],
        runner=_verify_runner({"verdict": "refuted", "rationale": "", "citations": []}),
    )
    out = capsys.readouterr().out
    assert code == 0  # "refuted" is a successful verification
    assert "recommendation.plan[0] — refuted" in out
    assert "rationale:" not in out


def test_main_verify_accepts_budget_and_runs_read_only(tmp_path):
    ws = _persisted_assessment(tmp_path)
    runner = _verify_runner()
    code = main(
        ["--verify", str(ws), "--finding", "pin build", "--budget-usd", "5"],
        runner=runner,
    )
    assert code == 0
    (call,) = runner.calls
    assert tuple(call["allowed_tools"]) == ("Read", "Grep", "Glob")
    assert call["cwd"] == str(ws.resolve())  # rooted at the assessed clone


def test_main_verify_agent_failure_exits_1(tmp_path, capsys):
    ws = _persisted_assessment(tmp_path)
    runner = ScriptedRunner(
        by_system_prompt={"application security engineer": "not json"}
    )
    code = main(["--verify", str(ws), "--finding", "pin build"], runner=runner)
    out = capsys.readouterr().out
    assert code == 1
    assert "verification failed:" in out
    # and the --json flavour keeps the exit code
    code = main(
        ["--verify", str(ws), "--finding", "pin build", "--json"],
        runner=ScriptedRunner(
            by_system_prompt={"application security engineer": "not json"}
        ),
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["success"] is False


def test_main_verify_missing_assessment_exits_2(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()  # the dir exists (so the isdir guard passes); it just has no assessment
    code = main(
        ["--verify", str(empty), "--finding", "x"],
        runner=ScriptedRunner([]),
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "no assessment.json" in captured.err
    assert "--assess" in captured.err
    assert captured.out == ""


def test_main_verify_unmatched_finding_exits_2(tmp_path, capsys):
    ws = _persisted_assessment(tmp_path)
    code = main(
        ["--verify", str(ws), "--finding", "no such claim"],
        runner=ScriptedRunner([]),
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "no finding matches" in captured.err
    assert "risk.secrets[0]" in captured.err  # the error teaches the id shape


def test_main_verify_missing_directory_exits_2(tmp_path, capsys):
    # F5: a mistyped --verify path must fail loudly, not silently mkdir an
    # empty workspace and then report a misleading "no assessment" error.
    missing = tmp_path / "nope"
    with pytest.raises(SystemExit) as excinfo:
        main(["--verify", str(missing), "--finding", "x"], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "no such directory" in capsys.readouterr().err
    assert not missing.exists()  # the typo did not create it


def test_main_verify_flag_validation():
    for argv in (
        ["--verify", ".", "--finding", "x", "--assess"],
        ["--verify", ".", "--finding", "x", "--deliver"],
        ["--verify", ".", "--finding", "x", "--chat"],
        ["--verify", ".", "--finding", "x", "--dashboard"],
        ["--verify", ".", "--finding", "x", "--dispatch"],
        ["--verify", ".", "--finding", "x", "--make-backlog", "."],
        ["--verify", "."],                       # --finding is required
        ["T", "D", "--finding", "x"],            # --finding needs --verify
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv, runner=ScriptedRunner([]))
        assert excinfo.value.code == 2


def test_main_verify_requires_credentials(monkeypatch, tmp_path, capsys):
    # Unlike --make-backlog, --verify RUNS AN AGENT → the preflight applies.
    _no_credentials(monkeypatch, tmp_path)
    ws = _persisted_assessment(tmp_path)
    code = main(["--verify", str(ws), "--finding", "pin build"])  # no runner
    captured = capsys.readouterr()
    assert code == 2
    assert "claude setup-token" in captured.err


def test_main_verify_builds_real_runner_when_none_injected(
    monkeypatch, tmp_path, capsys
):
    from dev_team.sdk import ClaudeAgentRunner

    ws = _persisted_assessment(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    seen = {}

    async def fake_verify(runner, workspace, finding, *, budget=None, **kwargs):
        seen["runner"] = runner
        return {
            "success": True,
            "verdict": "needs-context",
            "rationale": "",
            "citations": [],
            "finding_id": finding["id"],
            "source_job": None,
            "cost_usd": 0.0,
        }

    monkeypatch.setattr("dev_team.cli.verify_finding", fake_verify)
    code = main(["--verify", str(ws), "--finding", "pin build", "--model", "opus"])
    assert code == 0
    assert isinstance(seen["runner"], ClaudeAgentRunner)
    assert seen["runner"].default_model == "opus"
    assert "needs-context" in capsys.readouterr().out


# --- --repo / --env-file ----------------------------------------------------------


def _fake_clone_writing_dotnet(captured):
    from pathlib import Path

    def fake_clone(ref, dest, *, runner, token=None, timeout=None):
        captured.update(slug=ref.slug, dest=dest, token=token)
        target = Path(dest)
        (target / "src" / "Api").mkdir(parents=True, exist_ok=True)
        (target / "MyApp.sln").write_text("Microsoft Visual Studio Solution File")
        (target / "src" / "Api" / "Api.csproj").write_text("<Project/>")
        return dest

    return fake_clone


def test_main_repo_clones_with_env_file_token_then_assesses(
    tmp_path, monkeypatch, capsys
):
    from pathlib import Path

    from test_assessment import assess_responses

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=file-token\n")
    captured = {}
    monkeypatch.setattr(
        "dev_team.cli.clone_or_update", _fake_clone_writing_dotnet(captured)
    )
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--repo", "acme/mono", "--json"], runner=runner)
    assert code == 0
    assert captured["slug"] == "acme/mono"
    assert captured["dest"] == str(Path("./build") / "acme__mono")
    assert captured["token"] == "file-token"
    assert "fetching acme/mono" in capsys.readouterr().err


def test_main_repo_explicit_workspace_and_process_env_fallback(
    tmp_path, monkeypatch, capsys
):
    import os

    from test_assessment import assess_responses

    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-xdg"))
    monkeypatch.setenv("GITHUB_TOKEN", "proc-token")
    captured = {}
    monkeypatch.setattr(
        "dev_team.cli.clone_or_update", _fake_clone_writing_dotnet(captured)
    )
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(
        [
            "--assess", "--repo", "acme/mono",
            "--workspace", str(tmp_path / "here"), "--json",
        ],
        runner=runner,
    )
    assert code == 0
    assert captured["dest"] == str(tmp_path / "here")
    assert captured["token"] == "proc-token"
    # the token was consumed out of the environment the engines inherit
    assert "GITHUB_TOKEN" not in os.environ


def test_main_repo_requires_assess_deliver_or_chat():
    with pytest.raises(SystemExit) as excinfo:
        main(["Login", "Add login", "--repo", "acme/mono"], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2


def test_main_env_file_requires_repo():
    with pytest.raises(SystemExit) as excinfo:
        main(["--assess", "--env-file", ".env"], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2


def test_main_repo_invalid_ref_fails_cleanly(capsys):
    code = main(["--assess", "--repo", "%%%"], runner=ScriptedRunner([]))
    assert code == 2
    assert "unrecognised repository reference" in capsys.readouterr().err


def test_main_repo_finds_user_level_env_file_without_flags(
    tmp_path, monkeypatch, capsys
):
    from test_assessment import assess_responses

    monkeypatch.chdir(tmp_path)  # no ./.env
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    config = xdg / "dev-team" / "dev-team.env"
    config.parent.mkdir(parents=True)
    config.write_text("GITHUB_TOKEN=configured-once\n")
    captured = {}
    monkeypatch.setattr(
        "dev_team.cli.clone_or_update", _fake_clone_writing_dotnet(captured)
    )
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--repo", "acme/mono", "--json"], runner=runner)
    assert code == 0
    assert captured["token"] == "configured-once"
    assert f"env file: {config}" in capsys.readouterr().err


# --- --pull-request ---------------------------------------------------------------


def _fake_publish(seen, *, result=None, raises=None):
    """A stand-in for cli.publish_pull_request that records its kwargs."""

    from dev_team.pullrequest import PullRequest

    def publish(outcome, *, ref, token, git, publisher, base, draft):
        seen.update(
            slug=ref.slug, token=token, base=base, draft=draft,
            committed=outcome.committed, branch=outcome.branch,
        )
        if raises is not None:
            raise raises
        return result or PullRequest(3, "https://github.com/acme/mono/pull/3")

    return publish


def _fake_clone_empty_repo(ref, dest, *, runner, token=None, timeout=None):
    """A clone stand-in that yields a clean workspace (the engine inits git).

    Unlike a fake that writes untracked files into a non-git dir (which trips
    the dirty-baseline guard), an empty dir lets the engine ``git init`` a clean
    baseline — matching what a real clone hands the delivery.
    """

    from pathlib import Path

    Path(dest).mkdir(parents=True, exist_ok=True)
    return dest


def _pr_deliver(tmp_path, monkeypatch, seen, *extra, publish=None):
    """Drive `--deliver --repo --pull-request` with clone + publish faked."""

    import dev_team.cli as cli_module
    from helpers import engine_responses

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(cli_module, "clone_or_update", _fake_clone_empty_repo)
    monkeypatch.setattr(
        cli_module, "publish_pull_request", publish or _fake_publish(seen)
    )
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    return main(
        ["Health", "Add /health", "--deliver", "--repo", "acme/mono",
         "--pull-request", "--verify-command", "python -c pass", *extra],
        runner=runner,
    )


def test_main_deliver_pull_request_opens_pr(tmp_path, monkeypatch, capsys):
    seen = {}
    code = _pr_deliver(tmp_path, monkeypatch, seen)
    out = capsys.readouterr()
    assert code == 0
    # The CLI threaded the resolved ref + token (never re-resolved) into the
    # publish, with the default base and non-draft.
    assert seen["slug"] == "acme/mono"
    assert seen["token"] == "tok"
    assert seen["base"] == "main" and seen["draft"] is False
    # The URL is surfaced both as stderr progress and in the stdout summary.
    assert "opened pull request: https://github.com/acme/mono/pull/3" in out.err
    assert "Pull request: https://github.com/acme/mono/pull/3" in out.out


def test_main_deliver_pull_request_honours_base_and_draft(tmp_path, monkeypatch, capsys):
    seen = {}
    code = _pr_deliver(
        tmp_path, monkeypatch, seen, "--pr-base", "develop", "--pr-draft"
    )
    assert code == 0
    assert seen["base"] == "develop" and seen["draft"] is True


def test_main_deliver_pull_request_failure_sets_exit_code(tmp_path, monkeypatch, capsys):
    from dev_team.delivery_target import DeliveryTargetError

    seen = {}
    publish = _fake_publish(seen, raises=DeliveryTargetError("nothing to publish: no commit"))
    code = _pr_deliver(tmp_path, monkeypatch, seen, publish=publish)
    err = capsys.readouterr().err
    # The delivery itself succeeded, but the requested PR did not open, so the
    # exit code is non-zero and the reason is a clean line (no traceback).
    assert code == 1
    assert "pull request not opened: nothing to publish" in err


def test_main_pull_request_requires_deliver():
    with pytest.raises(SystemExit) as exc:
        main(["T", "D", "--pull-request"], runner=ScriptedRunner([]))
    assert exc.value.code == 2


def test_main_pull_request_requires_repo():
    with pytest.raises(SystemExit) as exc:
        main(["T", "D", "--deliver", "--pull-request"], runner=ScriptedRunner([]))
    assert exc.value.code == 2


def test_main_pull_request_incompatible_with_no_commit():
    with pytest.raises(SystemExit) as exc:
        main(
            ["T", "D", "--deliver", "--repo", "acme/mono", "--pull-request", "--no-commit"],
            runner=ScriptedRunner([]),
        )
    assert exc.value.code == 2


def test_main_pr_tuning_requires_pull_request():
    for extra in (["--pr-base", "develop"], ["--pr-draft"], ["--watch-checks"]):
        with pytest.raises(SystemExit) as exc:
            main(
                ["T", "D", "--deliver", "--repo", "acme/mono", *extra],
                runner=ScriptedRunner([]),
            )
        assert exc.value.code == 2


def test_main_checks_timeout_seconds_requires_watch_checks():
    with pytest.raises(SystemExit) as exc:
        main(
            ["T", "D", "--deliver", "--repo", "acme/mono", "--pull-request",
             "--checks-timeout-seconds", "60"],
            runner=ScriptedRunner([]),
        )
    assert exc.value.code == 2


# --- --watch-checks (issue #71) ---------------------------------------------------


def test_pull_request_without_watch_checks_never_constructs_check_runs_client(
    tmp_path, monkeypatch
):
    # [baseline] existing --pull-request behaviour is provably unchanged: no
    # GitHubCheckRunsClient is constructed and no check-runs HTTP call happens
    # unless --watch-checks was explicitly passed.
    import dev_team.cli as cli_module

    constructed = []

    class _Explosive:
        def __init__(self, *a, **kw):
            constructed.append((a, kw))

        def watch(self, *a, **kw):
            raise AssertionError("GitHubCheckRunsClient.watch must not be called")

    monkeypatch.setattr(cli_module, "GitHubCheckRunsClient", _Explosive)
    seen = {}
    code = _pr_deliver(tmp_path, monkeypatch, seen)
    assert code == 0
    assert constructed == []


def test_watch_checks_reuses_the_pull_request_token_no_new_credential(
    tmp_path, monkeypatch
):
    # [security] the watch path is constructed with only the token already
    # resolved for --pull-request (GITHUB_TOKEN); no new env var, credential
    # file, or config key is read to build it.
    import dev_team.cli as cli_module
    from dev_team.pullrequest import CheckRunsResult

    seen_tokens = []

    class _FakeCheckRunsClient:
        def __init__(self, token):
            seen_tokens.append(token)

        def watch(self, owner, name, ref, *, timeout_seconds):
            return CheckRunsResult(state="success")

    monkeypatch.setattr(cli_module, "GitHubCheckRunsClient", _FakeCheckRunsClient)
    seen = {}
    code = _pr_deliver(tmp_path, monkeypatch, seen, "--watch-checks")
    assert code == 0
    assert seen_tokens == ["tok"]  # the exact token _pr_deliver resolved for --pull-request


def test_main_deliver_watch_checks_surfaces_state(tmp_path, monkeypatch, capsys):
    import dev_team.cli as cli_module
    from dev_team.pullrequest import DEFAULT_CHECKS_TIMEOUT_SECONDS, CheckRunsResult

    class _FakeCheckRunsClient:
        def __init__(self, token):
            pass

        def watch(self, owner, name, ref, *, timeout_seconds):
            assert timeout_seconds == DEFAULT_CHECKS_TIMEOUT_SECONDS
            return CheckRunsResult(
                state="failure",
                check_runs=[{"name": "ci", "status": "completed", "conclusion": "failure"}],
            )

    monkeypatch.setattr(cli_module, "GitHubCheckRunsClient", _FakeCheckRunsClient)
    seen = {}
    code = _pr_deliver(tmp_path, monkeypatch, seen, "--watch-checks", "--json")
    out = capsys.readouterr()
    # A failed/red watch never flips the exit code — the PR itself opened fine.
    assert code == 0
    assert "PR checks: failure" in out.err
    data = json.loads(out.out)
    assert data["pull_request_checks"]["state"] == "failure"
    assert data["pull_request_checks"]["failing_checks"] == ["ci"]


def test_main_deliver_watch_checks_honours_custom_timeout(tmp_path, monkeypatch):
    import dev_team.cli as cli_module
    from dev_team.pullrequest import CheckRunsResult

    captured = {}

    class _FakeCheckRunsClient:
        def __init__(self, token):
            pass

        def watch(self, owner, name, ref, *, timeout_seconds):
            captured["timeout_seconds"] = timeout_seconds
            return CheckRunsResult(state="success")

    monkeypatch.setattr(cli_module, "GitHubCheckRunsClient", _FakeCheckRunsClient)
    seen = {}
    code = _pr_deliver(
        tmp_path, monkeypatch, seen, "--watch-checks", "--checks-timeout-seconds", "42"
    )
    assert code == 0
    assert captured["timeout_seconds"] == 42.0


def test_main_deliver_watch_checks_transport_failure_never_affects_exit_code(
    tmp_path, monkeypatch
):
    # [security/fail-secure] end-to-end through the REAL GitHubCheckRunsClient
    # (CLI constructs it with no injected http, so this exercises the default
    # transport): a network failure while watching is caught inside .watch()
    # itself and must never flip an already-successful PR-open's exit code.
    import urllib.error

    import dev_team.pullrequest as pr_module

    def raising_http_get(url, headers):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(pr_module, "_http_get", raising_http_get)
    seen = {}
    code = _pr_deliver(tmp_path, monkeypatch, seen, "--watch-checks")
    assert code == 0


def test_main_deliver_watch_checks_malformed_body_never_affects_exit_code(
    tmp_path, monkeypatch
):
    # [security/fail-secure] a malformed (non-JSON, or syntactically-valid but
    # non-dict) response body must be caught inside .watch() itself — not
    # escape as a raw ValueError/AttributeError and turn an already-successful
    # PR-open into a crashed `error:` exit code (BPG §4: never trust upstream
    # output).
    import dev_team.pullrequest as pr_module

    def bad_json_http_get(url, headers):
        raise json.JSONDecodeError("Expecting value", "<html>not json</html>", 0)

    monkeypatch.setattr(pr_module, "_http_get", bad_json_http_get)
    seen = {}
    code = _pr_deliver(tmp_path, monkeypatch, seen, "--watch-checks")
    assert code == 0


def test_main_chat_deliver_pull_request_threads_ref_and_token(
    tmp_path, monkeypatch, capsys
):
    # Regression: an in-session /deliver must receive the clone's resolved
    # ref/token so --pull-request works from chat too. They were dropped before,
    # so publish saw ref=None/token="" and failed with a misleading
    # "no token resolved" error even though the clone had just used one.
    import io as _io

    import dev_team.cli as cli_module
    from helpers import engine_responses
    from test_chat import FakeBackend

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(cli_module, "clone_or_update", _fake_clone_empty_repo)
    seen = {}
    monkeypatch.setattr(cli_module, "publish_pull_request", _fake_publish(seen))
    monkeypatch.setattr("sys.stdin", _io.StringIO("/deliver\ny\n/quit\n"))
    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(
        ["--chat", "--repo", "acme/mono", "--pull-request",
         "--verify-command", "python -c pass"],
        runner=runner,
        chat_backend=FakeBackend(),
    )
    assert code == 0
    assert seen["slug"] == "acme/mono"
    assert seen["token"] == "tok"


# --- dashboard --------------------------------------------------------------------


class _FakeDashboardServer:
    instances = []

    def __init__(self, workspace, *, host, port, token=None,
                 dispatch_url=None, dispatch_token=None):
        self.workspace, self.host, self.port = workspace, host, port
        self.token = token
        self.dispatch_url = dispatch_url
        self.dispatch_token = dispatch_token
        self.interrupted = False
        self.shut_down = False
        _FakeDashboardServer.instances.append(self)

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/"

    def serve_forever(self):
        if self.interrupted:
            raise KeyboardInterrupt

    def shutdown(self):
        self.shut_down = True


def test_main_dashboard_serves_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    _FakeDashboardServer.instances.clear()
    monkeypatch.delenv("DEV_TEAM_DASHBOARD_TOKEN", raising=False)
    code = main(
        ["--dashboard", "--workspace", str(tmp_path), "--port", "9000",
         "--host", "0.0.0.0"],
        runner=ScriptedRunner([]),
    )
    assert code == 0
    (server,) = _FakeDashboardServer.instances
    assert (server.host, server.port) == ("0.0.0.0", 9000)
    assert str(server.workspace.root) == str(tmp_path)
    assert server.shut_down is True
    assert "http://0.0.0.0:9000/" in capsys.readouterr().err


def test_main_dashboard_defaults_and_ctrl_c(tmp_path, monkeypatch):
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    _FakeDashboardServer.instances.clear()

    original_init = _FakeDashboardServer.__init__

    def interrupting_init(self, workspace, *, host, port, token=None,
                          dispatch_url=None, dispatch_token=None):
        original_init(self, workspace, host=host, port=port, token=token,
                      dispatch_url=dispatch_url, dispatch_token=dispatch_token)
        self.interrupted = True

    monkeypatch.setattr(_FakeDashboardServer, "__init__", interrupting_init)
    code = main(["--dashboard", "--workspace", str(tmp_path)], runner=None)
    assert code == 0  # KeyboardInterrupt is a clean stop, and no credentials needed
    (server,) = _FakeDashboardServer.instances
    assert (server.host, server.port) == ("127.0.0.1", 8737)
    assert server.shut_down is True


def test_main_dashboard_token_env_is_wired(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    _FakeDashboardServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DASHBOARD_TOKEN", "dash-tok")
    code = main(
        ["--dashboard", "--workspace", str(tmp_path), "--host", "100.64.0.1"],
        runner=None,
    )
    assert code == 0
    (server,) = _FakeDashboardServer.instances
    assert server.token == "dash-tok"
    err = capsys.readouterr().err
    assert "UNAUTHENTICATED" not in err  # a token silences the non-local nudge
    assert "dash-tok" not in err  # the token itself is never printed


def test_main_dashboard_warns_on_nonlocal_bind_without_token(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    _FakeDashboardServer.instances.clear()
    monkeypatch.delenv("DEV_TEAM_DASHBOARD_TOKEN", raising=False)
    code = main(
        ["--dashboard", "--workspace", str(tmp_path), "--host", "0.0.0.0"],
        runner=None,
    )
    assert code == 0  # a nudge, not a hard failure (back-compat)
    (server,) = _FakeDashboardServer.instances
    assert server.token is None
    err = capsys.readouterr().err
    assert "UNAUTHENTICATED" in err
    assert "DEV_TEAM_DASHBOARD_TOKEN" in err


def test_main_dashboard_local_bind_without_token_is_quiet(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    monkeypatch.delenv("DEV_TEAM_DASHBOARD_TOKEN", raising=False)
    for host_args in ([], ["--host", "127.0.0.1"], ["--host", "localhost"]):
        _FakeDashboardServer.instances.clear()
        code = main(
            ["--dashboard", "--workspace", str(tmp_path), *host_args],
            runner=None,
        )
        assert code == 0
        (server,) = _FakeDashboardServer.instances
        assert server.token is None
        assert "UNAUTHENTICATED" not in capsys.readouterr().err


def test_main_dashboard_missing_workspace_exits_2(tmp_path, capsys):
    # F5: --dashboard is a viewer; a mistyped --workspace must fail loudly
    # rather than silently create an empty directory to serve.
    missing = tmp_path / "nope"
    with pytest.raises(SystemExit) as excinfo:
        main(["--dashboard", "--workspace", str(missing)], runner=ScriptedRunner([]))
    assert excinfo.value.code == 2
    assert "no such directory" in capsys.readouterr().err
    assert not missing.exists()


def test_main_dashboard_warns_when_transcripts_present_without_token(
    tmp_path, monkeypatch, capsys
):
    # F9: even on loopback, an unauthenticated dashboard over a workspace that
    # already holds transcripts exposes raw prompts/responses — warn (no fail).
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    _FakeDashboardServer.instances.clear()
    monkeypatch.delenv("DEV_TEAM_DASHBOARD_TOKEN", raising=False)
    tdir = tmp_path / ".dev_team" / "transcripts" / "deliver-x"
    tdir.mkdir(parents=True)
    (tdir / "engineer-001.json").write_text("{}")
    code = main(["--dashboard", "--workspace", str(tmp_path)], runner=None)
    assert code == 0  # a warning, not a hard failure
    err = capsys.readouterr().err
    assert "UNAUTHENTICATED" in err
    assert "transcript" in err.lower()


def test_main_dashboard_flag_validation():
    for argv in (
        ["--dashboard", "--assess"],
        ["--dashboard", "--deliver", "T", "D"],
        ["--dashboard", "--chat"],
        ["T", "D", "--port", "9000"],
        ["T", "D", "--host", "0.0.0.0"],
        ["T", "D", "--dispatch-url", "http://127.0.0.1:8738"],
        ["--dispatch", "--dispatch-url", "http://127.0.0.1:8738"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv, runner=ScriptedRunner([]))
        assert excinfo.value.code == 2


def test_main_dashboard_wires_the_board_write_proxy(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    _FakeDashboardServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DISPATCH_TOKEN", "dispatch-tok")
    code = main(
        ["--dashboard", "--workspace", str(tmp_path),
         "--dispatch-url", "http://100.64.0.9:8738"],
        runner=None,
    )
    assert code == 0
    (server,) = _FakeDashboardServer.instances
    assert server.dispatch_url == "http://100.64.0.9:8738"
    assert server.dispatch_token == "dispatch-tok"
    assert "dispatch-tok" not in capsys.readouterr().err  # never printed


def test_main_dashboard_dispatch_url_defaults_and_empty_token_is_none(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("dev_team.cli.DashboardServer", _FakeDashboardServer)
    _FakeDashboardServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DISPATCH_TOKEN", "")
    code = main(["--dashboard", "--workspace", str(tmp_path)], runner=None)
    assert code == 0
    (server,) = _FakeDashboardServer.instances
    # the default dispatch URL is wired, but with no token the proxy stays
    # disabled (None, never the empty string)
    assert server.dispatch_url == "http://127.0.0.1:8738"
    assert server.dispatch_token is None
    assert "dispatch-tok" not in capsys.readouterr().err


# --- dispatch service -------------------------------------------------------------


class _FakeDispatchServer:
    instances = []

    def __init__(self, token, *, host, port, runner=None, dashboard_workspace=None,
                 record_transcripts=False):
        self.token, self.host, self.port, self.runner = token, host, port, runner
        self.dashboard_workspace = dashboard_workspace
        self.record_transcripts = record_transcripts
        self.interrupted = False
        self.shut_down = False
        _FakeDispatchServer.instances.append(self)

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/"

    def serve_forever(self):
        if self.interrupted:
            raise KeyboardInterrupt

    def shutdown(self):
        self.shut_down = True


def test_main_dispatch_serves_with_env_token(monkeypatch, capsys):
    monkeypatch.setattr("dev_team.cli.DispatchServer", _FakeDispatchServer)
    _FakeDispatchServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DISPATCH_TOKEN", "tok")
    code = main(
        ["--dispatch", "--host", "100.64.0.1", "--port", "8738"],
        runner=ScriptedRunner([]),
    )
    assert code == 0
    (server,) = _FakeDispatchServer.instances
    assert (server.host, server.port) == ("100.64.0.1", 8738)
    assert server.token == "tok"
    assert server.runner is not None  # the injected runner is threaded through
    assert server.shut_down is True
    assert "dispatch service at" in capsys.readouterr().err


def test_main_dispatch_defaults_and_ctrl_c(monkeypatch):
    monkeypatch.setattr("dev_team.cli.DispatchServer", _FakeDispatchServer)
    _FakeDispatchServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DISPATCH_TOKEN", "tok")

    original_init = _FakeDispatchServer.__init__

    def interrupting_init(self, token, *, host, port, runner=None,
                          dashboard_workspace=None, record_transcripts=False):
        original_init(self, token, host=host, port=port, runner=runner,
                      dashboard_workspace=dashboard_workspace,
                      record_transcripts=record_transcripts)
        self.interrupted = True

    monkeypatch.setattr(_FakeDispatchServer, "__init__", interrupting_init)
    code = main(["--dispatch"], runner=ScriptedRunner([]))
    assert code == 0  # KeyboardInterrupt is a clean stop
    (server,) = _FakeDispatchServer.instances
    assert (server.host, server.port) == ("127.0.0.1", 8738)
    assert server.dashboard_workspace is None  # not requested → not wired
    assert server.record_transcripts is False  # off by default
    assert server.shut_down is True


def test_main_dispatch_record_transcripts_flag(monkeypatch):
    monkeypatch.setattr("dev_team.cli.DispatchServer", _FakeDispatchServer)
    _FakeDispatchServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DISPATCH_TOKEN", "tok")
    monkeypatch.delenv("DEV_TEAM_RECORD_TRANSCRIPTS", raising=False)
    assert main(["--dispatch", "--record-transcripts"], runner=ScriptedRunner([])) == 0
    (server,) = _FakeDispatchServer.instances
    assert server.record_transcripts is True


def test_main_dispatch_record_transcripts_env(monkeypatch):
    monkeypatch.setattr("dev_team.cli.DispatchServer", _FakeDispatchServer)
    _FakeDispatchServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DISPATCH_TOKEN", "tok")
    monkeypatch.setenv("DEV_TEAM_RECORD_TRANSCRIPTS", "TRUE")  # case-insensitive
    assert main(["--dispatch"], runner=ScriptedRunner([])) == 0
    (server,) = _FakeDispatchServer.instances
    assert server.record_transcripts is True


def test_main_record_transcripts_requires_a_run_mode():
    # Meaningless without --assess/--deliver/--dispatch → argparse error.
    with pytest.raises(SystemExit) as excinfo:
        main(["--record-transcripts", "--dashboard", "--workspace", "."],
             runner=ScriptedRunner([]))
    assert excinfo.value.code == 2


def test_main_record_transcripts_rejected_with_chat(capsys):
    # F4: a chat has no run id / event log, so recording captures nothing —
    # reject it rather than silently accept and record nothing.
    with pytest.raises(SystemExit) as excinfo:
        main(["--chat", "--record-transcripts"], runner=ScriptedRunner([]))
    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    assert "--record-transcripts" in err
    # the message names the modes that actually support it, and not --chat
    assert "--assess/--deliver/--dispatch" in err


def test_main_dispatch_requires_token(monkeypatch, capsys):
    monkeypatch.delenv("DEV_TEAM_DISPATCH_TOKEN", raising=False)
    code = main(["--dispatch"], runner=ScriptedRunner([]))
    assert code == 2
    assert "DEV_TEAM_DISPATCH_TOKEN" in capsys.readouterr().err


def test_main_dispatch_dashboard_workspace_is_wired(monkeypatch, tmp_path):
    monkeypatch.setattr("dev_team.cli.DispatchServer", _FakeDispatchServer)
    _FakeDispatchServer.instances.clear()
    monkeypatch.setenv("DEV_TEAM_DISPATCH_TOKEN", "tok")
    dash = tmp_path / "shared-workspace"
    code = main(
        ["--dispatch", "--dashboard-workspace", str(dash)],
        runner=ScriptedRunner([]),
    )
    assert code == 0
    (server,) = _FakeDispatchServer.instances
    from dev_team.execution import LocalWorkspace

    assert isinstance(server.dashboard_workspace, LocalWorkspace)
    assert str(server.dashboard_workspace.root) == str(dash)


def test_main_dashboard_workspace_requires_dispatch():
    # --dashboard-workspace is meaningless without --dispatch → argparse error.
    with pytest.raises(SystemExit) as excinfo:
        main(["--dashboard", "--dashboard-workspace", "/tmp/x", "--workspace", "."],
             runner=ScriptedRunner([]))
    assert excinfo.value.code == 2


def test_main_dispatch_flag_validation():
    for argv in (
        ["--dispatch", "--assess"],
        ["--dispatch", "--deliver", "T", "D"],
        ["--dispatch", "--chat"],
        ["--dispatch", "--dashboard"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv, runner=ScriptedRunner([]))
        assert excinfo.value.code == 2


def test_main_deliver_journals_events_for_the_dashboard(tmp_path):
    from helpers import engine_responses

    from dev_team.eventlog import EVENTS_PATH

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(_deliver_args(tmp_path), runner=runner)
    assert code == 0
    journal = (tmp_path / EVENTS_PATH).read_text().splitlines()
    records = [json.loads(line) for line in journal]
    assert records, "delivery left no event journal"
    assert all(r["run"].startswith("deliver-") for r in records)
    assert {"engineer", "reviewer"} <= {r["role"] for r in records}


def test_main_deliver_records_transcripts_under_the_events_run_id(tmp_path):
    from helpers import engine_responses

    from dev_team.eventlog import EVENTS_PATH
    from dev_team.execution import LocalWorkspace
    from dev_team.transcripts import list_transcripts

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    code = main(_deliver_args(tmp_path, "--record-transcripts"), runner=runner)
    assert code == 0
    # the recorder shares the SAME run id as the event journal
    records = [json.loads(line) for line in (tmp_path / EVENTS_PATH).read_text().splitlines()]
    run_id = records[0]["run"]
    ws = LocalWorkspace(str(tmp_path), excluded_dirs=frozenset())
    assert list_transcripts(ws, run_id, "engineer")


def test_main_deliver_records_no_transcripts_by_default(tmp_path):
    from helpers import engine_responses

    from dev_team.transcripts import TRANSCRIPTS_DIR

    runner = ScriptedRunner(by_system_prompt=engine_responses())
    assert main(_deliver_args(tmp_path), runner=runner) == 0
    assert not (tmp_path / TRANSCRIPTS_DIR).exists()


def test_main_assess_records_transcripts(tmp_path):
    from test_assessment import assess_responses

    from dev_team.eventlog import EVENTS_PATH
    from dev_team.execution import LocalWorkspace
    from dev_team.transcripts import list_transcripts

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--workspace", str(repo), "--record-transcripts"], runner=runner)
    assert code == 0
    records = [json.loads(line) for line in (repo / EVENTS_PATH).read_text().splitlines()]
    run_id = records[0]["run"]
    ws = LocalWorkspace(str(repo), excluded_dirs=frozenset())
    assert list_transcripts(ws, run_id, "architect")


def test_main_assess_journals_events_for_the_dashboard(tmp_path):
    from test_assessment import assess_responses

    from dev_team.eventlog import EVENTS_PATH

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--workspace", str(repo)], runner=runner)
    assert code == 0
    records = [
        json.loads(line)
        for line in (repo / EVENTS_PATH).read_text().splitlines()
    ]
    assert any(r["run"].startswith("assess-") for r in records)


def test_sandbox_config_from_flags():
    from dev_team.cli import _sandbox_config

    parser = build_parser()
    args = parser.parse_args(
        [
            "--deliver", "--sandbox",
            "--sandbox-image", "node:22",
            "--sandbox-network", "bridge",
            "--sandbox-engine", "podman",
            "T", "D",
        ]
    )
    sc = _sandbox_config(args)
    assert (sc.engine, sc.image, sc.network) == ("podman", "node:22", "bridge")


def test_sandbox_config_absent_and_defaults():
    from dev_team.cli import _sandbox_config

    parser = build_parser()
    assert _sandbox_config(parser.parse_args(["--deliver", "T", "D"])) is None
    sc = _sandbox_config(parser.parse_args(["--deliver", "--sandbox", "T", "D"]))
    assert sc is not None
    assert sc.network == "none"  # secure default preserved when not overridden


def test_sandbox_wired_into_engine_config():
    from dev_team.cli import _engine_config

    parser = build_parser()
    args = parser.parse_args(["--deliver", "--sandbox", "T", "D"])
    assert _engine_config(args).sandbox is not None


def test_main_sandbox_without_mode_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["T", "D", "--sandbox"], runner=ScriptedRunner([]))
    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    assert "--sandbox" in err


def test_main_sandbox_tuning_without_sandbox_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(
            ["T", "D", "--deliver", "--sandbox-image", "x"],
            runner=ScriptedRunner([]),
        )
    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    assert "--sandbox-image" in err


def test_main_assess_with_sandbox_succeeds(tmp_path):
    from test_assessment import assess_responses

    repo = _dotnet_repo(tmp_path)
    runner = ScriptedRunner(by_system_prompt=assess_responses())
    code = main(["--assess", "--workspace", str(repo), "--sandbox"], runner=runner)
    assert code == 0
    assert (repo / "audit" / "assessment.md").exists()
