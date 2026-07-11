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
