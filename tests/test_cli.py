"""Tests for the command-line interface."""

from __future__ import annotations

import json

from helpers import happy_responses, json_response, plan_dict, design_dict, impl_dict
from helpers import review_dict, deploy_dict

from dev_team.cli import build_parser, main
from dev_team.testing import ScriptedRunner


def test_build_parser_parses_constraints():
    parser = build_parser()
    args = parser.parse_args(["Title", "Desc", "-c", "one", "-c", "two"])
    assert args.title == "Title"
    assert args.constraints == ["one", "two"]


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


def test_main_verbose_prints_events(capsys):
    runner = ScriptedRunner(happy_responses(1))
    code = main(["Login", "Add login", "--verbose"], runner=runner)
    out = capsys.readouterr().out
    assert code == 0
    assert "[workflow/" in out


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


def test_main_invalid_config_returns_error(capsys):
    code = main(["Login", "Add login", "--max-attempts", "0"], runner=ScriptedRunner([]))
    out = capsys.readouterr().out
    assert code == 2
    assert "error:" in out
