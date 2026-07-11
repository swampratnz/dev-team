"""Tests for personas and the roster."""

from __future__ import annotations

import json

import pytest

from helpers import happy_responses, run

from dev_team.agents.base import BaseAgent
from dev_team.errors import DevTeamError
from dev_team.persona import DEFAULT_CAST, Persona, Roster
from dev_team.team import DevTeam, build_workflow
from dev_team.testing import ScriptedRunner


# --- Persona ---------------------------------------------------------------


def test_persona_preamble_with_style():
    p = Persona(name="Ada", role="engineer", style="You are terse.")
    assert p.preamble() == "Your name is Ada. You are terse."


def test_persona_preamble_without_style():
    p = Persona(name="Ada", role="engineer")
    assert p.preamble() == "Your name is Ada."


def test_persona_rejects_empty_name():
    with pytest.raises(ValueError):
        Persona(name="  ", role="engineer")


def test_persona_rejects_empty_role():
    with pytest.raises(ValueError):
        Persona(name="Ada", role="")


# --- Roster ------------------------------------------------------------------


def test_default_roster_casts_every_role():
    roster = Roster.default()
    for role in DEFAULT_CAST:
        persona = roster.get(role)
        assert persona is not None
        assert persona.role == role
        assert persona.name
        assert persona.style


def test_anonymous_roster_has_no_personas():
    roster = Roster.anonymous()
    assert roster.get("engineer") is None
    assert roster.display_name("engineer") == "engineer"


def test_display_name_uses_persona_when_cast():
    assert Roster.default().display_name("engineer") == "Sam"


def test_from_dict_overlays_default_cast():
    roster = Roster.from_dict({"engineer": {"name": "Ada", "style": "You are terse."}})
    assert roster.get("engineer").name == "Ada"
    assert roster.get("engineer").style == "You are terse."
    # untouched roles keep the default cast
    assert roster.get("reviewer").name == DEFAULT_CAST["reviewer"].name


def test_from_dict_keeps_default_style_when_omitted():
    roster = Roster.from_dict({"engineer": {"name": "Ada"}})
    assert roster.get("engineer").style == DEFAULT_CAST["engineer"].style


def test_from_dict_rejects_unknown_role():
    with pytest.raises(DevTeamError) as excinfo:
        Roster.from_dict({"architekt": {"name": "Typo"}})
    assert "architekt" in str(excinfo.value)


def test_from_dict_rejects_non_object_entry():
    with pytest.raises(DevTeamError):
        Roster.from_dict({"engineer": "Ada"})


def test_from_dict_rejects_missing_name():
    with pytest.raises(DevTeamError):
        Roster.from_dict({"engineer": {"style": "You are terse."}})


def test_from_dict_rejects_non_string_style():
    with pytest.raises(DevTeamError):
        Roster.from_dict({"engineer": {"name": "Ada", "style": 3}})


def test_from_file_loads_overlay(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"qa": {"name": "Vera"}}))
    roster = Roster.from_file(str(path))
    assert roster.get("qa").name == "Vera"


def test_from_file_missing_file(tmp_path):
    with pytest.raises(DevTeamError) as excinfo:
        Roster.from_file(str(tmp_path / "nope.json"))
    assert "cannot read" in str(excinfo.value)


def test_from_file_invalid_json(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text("{not json")
    with pytest.raises(DevTeamError) as excinfo:
        Roster.from_file(str(path))
    assert "not valid JSON" in str(excinfo.value)


def test_from_file_non_object(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text("[1, 2]")
    with pytest.raises(DevTeamError) as excinfo:
        Roster.from_file(str(path))
    assert "JSON object" in str(excinfo.value)


# --- persona wiring into agents ---------------------------------------------


class _EchoAgent(BaseAgent):
    role = "engineer"
    stage = "work"
    system_prompt = "You are a senior software engineer."


def test_agent_system_prompt_gets_persona_preamble():
    runner = ScriptedRunner(["ok"])
    agent = _EchoAgent(runner, persona=Persona(name="Ada", role="engineer", style="You are terse."))
    run(agent.ask("hello"))
    sent = runner.calls[0]["system_prompt"]
    assert sent.startswith("Your name is Ada. You are terse.")
    assert sent.endswith("You are a senior software engineer.")


def test_agent_system_prompt_unchanged_without_persona():
    runner = ScriptedRunner(["ok"])
    agent = _EchoAgent(runner)
    run(agent.ask("hello"))
    assert runner.calls[0]["system_prompt"] == "You are a senior software engineer."


def test_agent_events_carry_persona_name():
    events = []
    runner = ScriptedRunner(["ok"])
    agent = _EchoAgent(
        runner,
        listener=events.append,
        persona=Persona(name="Ada", role="engineer"),
    )
    run(agent.ask("hello"))
    assert events and all(e.name == "Ada" for e in events)
    assert "[Ada (engineer)/work]" in str(events[0])


def test_agent_events_have_no_name_without_persona():
    events = []
    runner = ScriptedRunner(["ok"])
    agent = _EchoAgent(runner, listener=events.append)
    run(agent.ask("hello"))
    assert events and all(e.name is None for e in events)
    assert "[engineer/work]" in str(events[0])


# --- roster wiring through the facade ----------------------------------------


def test_build_workflow_casts_default_personas():
    workflow = build_workflow(ScriptedRunner([]))
    assert workflow.manager.persona.name == "Priya"
    assert workflow.engineer.persona.name == "Sam"


def test_build_workflow_honours_anonymous_roster():
    workflow = build_workflow(ScriptedRunner([]), roster=Roster.anonymous())
    assert workflow.manager.persona is None


def test_devteam_events_show_persona_names():
    events = []
    team = DevTeam(ScriptedRunner(happy_responses(1)), listener=events.append)
    result = run(team.develop_feature("Login", "Add login"))
    assert result.success
    names = {e.name for e in events if e.name}
    assert "Priya" in names and "Sam" in names


def test_devteam_engine_inherits_roster():
    team = DevTeam(ScriptedRunner([]), roster=Roster.anonymous())
    engine = team.make_engine()
    assert engine.manager.persona is None
    assert engine.roster.get("engineer") is None


def test_engine_defaults_to_default_cast():
    team = DevTeam(ScriptedRunner([]))
    engine = team.make_engine()
    assert engine.security.persona.name == "Sasha"
