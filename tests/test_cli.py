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


# --- dashboard --------------------------------------------------------------------


class _FakeDashboardServer:
    instances = []

    def __init__(self, workspace, *, host, port):
        self.workspace, self.host, self.port = workspace, host, port
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

    def interrupting_init(self, workspace, *, host, port):
        original_init(self, workspace, host=host, port=port)
        self.interrupted = True

    monkeypatch.setattr(_FakeDashboardServer, "__init__", interrupting_init)
    code = main(["--dashboard", "--workspace", str(tmp_path)], runner=None)
    assert code == 0  # KeyboardInterrupt is a clean stop, and no credentials needed
    (server,) = _FakeDashboardServer.instances
    assert (server.host, server.port) == ("127.0.0.1", 8737)
    assert server.shut_down is True


def test_main_dashboard_flag_validation():
    for argv in (
        ["--dashboard", "--assess"],
        ["--dashboard", "--deliver", "T", "D"],
        ["--dashboard", "--chat"],
        ["T", "D", "--port", "9000"],
        ["T", "D", "--host", "0.0.0.0"],
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
