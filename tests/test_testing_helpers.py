"""Tests for the ScriptedRunner test double."""

from __future__ import annotations

import json

import pytest
from helpers import run

from dev_team.sdk import AgentResult
from dev_team.testing import ScriptedRunner, json_response


def test_json_response_is_extractable():
    text = json_response({"a": 1})
    assert json.loads(text[text.index("{") : text.index("}") + 1]) == {"a": 1}


def test_queue_consumed_in_order():
    runner = ScriptedRunner(["first", "second"])
    assert run(runner.run("p")).text == "first"
    assert run(runner.run("p")).text == "second"


def test_add_returns_self_for_chaining():
    runner = ScriptedRunner()
    assert runner.add("a").add("b") is runner
    assert run(runner.run("p")).text == "a"


def test_keyed_response_takes_priority():
    runner = ScriptedRunner(
        ["fallback"], by_system_prompt={"architect": "keyed"}
    )
    result = run(runner.run("p", system_prompt="you are an architect"))
    assert result.text == "keyed"
    # The queue is untouched when a keyed response matched.
    assert run(runner.run("p")).text == "fallback"


def test_keyed_miss_falls_back_to_queue():
    runner = ScriptedRunner(["fallback"], by_system_prompt={"architect": "keyed"})
    result = run(runner.run("p", system_prompt="you are a reviewer"))
    assert result.text == "fallback"


def test_system_prompt_none_skips_keyed():
    runner = ScriptedRunner(["fallback"], by_system_prompt={"x": "keyed"})
    assert run(runner.run("p")).text == "fallback"


def test_agent_result_passthrough():
    canned = AgentResult(text="pre-made", num_turns=9)
    runner = ScriptedRunner([canned])
    assert run(runner.run("p")) is canned


def test_out_of_responses_raises():
    runner = ScriptedRunner([])
    with pytest.raises(AssertionError, match="ran out"):
        run(runner.run("p"))


def test_calls_are_recorded():
    runner = ScriptedRunner(["x"])
    run(runner.run("prompt", system_prompt="sys", allowed_tools=["Read"], model="m"))
    assert runner.calls[0]["prompt"] == "prompt"
    assert runner.calls[0]["allowed_tools"] == ["Read"]
