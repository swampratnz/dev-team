"""Tests for the Claude Agent SDK adapter."""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import ProcessError
from helpers import run

from dev_team import sdk
from dev_team.sdk import (
    AgentResult,
    AgentRunner,
    ClaudeAgentRunner,
    build_options,
    extract_text,
)
from dev_team.testing import ScriptedRunner


# --- fake SDK message objects -------------------------------------------


class Block:
    def __init__(self, text):
        self.text = text


class NoTextBlock:
    """A content block without a text attribute (e.g. a tool use)."""


class Assistant:
    def __init__(self, content, model=None):
        self.content = content
        self.model = model


class ResultMsg:
    def __init__(self, total_cost_usd, num_turns, is_error, result):
        self.total_cost_usd = total_cost_usd
        self.num_turns = num_turns
        self.is_error = is_error
        self.result = result


def _fake_query(messages):
    async def query(*, prompt, options):  # noqa: ANN001
        for message in messages:
            yield message

    return query


# --- AgentResult / helpers ----------------------------------------------


def test_agent_result_defaults():
    result = AgentResult(text="hi")
    assert result.cost_usd == 0.0
    assert result.num_turns == 0
    assert result.model is None
    assert result.is_error is False


def test_build_options_minimal():
    options = build_options(
        system_prompt=None,
        allowed_tools=None,
        model=None,
        permission_mode="bypassPermissions",
        cwd=None,
        max_turns=None,
    )
    assert options.permission_mode == "bypassPermissions"
    assert options.system_prompt is None
    # None means an explicit empty allowlist, never "no restriction".
    assert options.allowed_tools == []


def test_build_options_full():
    options = build_options(
        system_prompt="sys",
        allowed_tools=["Read", "Write"],
        model="claude-x",
        permission_mode="acceptEdits",
        cwd="/tmp",
        max_turns=5,
    )
    assert options.system_prompt == "sys"
    assert options.allowed_tools == ["Read", "Write"]
    assert options.model == "claude-x"
    assert options.cwd == "/tmp"
    assert options.max_turns == 5


def test_extract_text_variants():
    assert extract_text(Assistant([Block("a"), NoTextBlock(), Block("b")])) == [
        "a",
        "b",
    ]
    # No content attribute at all.
    assert extract_text(object()) == []
    # Content present but not a list.
    assert extract_text(Assistant(content="not-a-list")) == []


# --- ClaudeAgentRunner --------------------------------------------------


def test_runner_full_flow(monkeypatch):
    messages = [
        Assistant([Block("hello")], model="claude-real"),
        ResultMsg(total_cost_usd=0.5, num_turns=3, is_error=False, result="final"),
    ]
    monkeypatch.setattr(sdk, "query", _fake_query(messages))

    runner = ClaudeAgentRunner(default_model="default-model")
    result = run(runner.run("prompt", system_prompt="sys", allowed_tools=["Read"]))

    assert result.text == "hello\nfinal"
    assert result.cost_usd == 0.5
    assert result.num_turns == 3
    assert result.model == "claude-real"
    assert result.is_error is False
    assert runner._last_options is not None


def test_runner_minimal_flow(monkeypatch):
    messages = [
        Assistant([Block("x")], model=None),
        ResultMsg(total_cost_usd=None, num_turns=0, is_error=False, result="   "),
    ]
    monkeypatch.setattr(sdk, "query", _fake_query(messages))

    runner = ClaudeAgentRunner()
    result = run(runner.run("prompt", model="override"))

    # Empty/whitespace result text is not appended.
    assert result.text == "x"
    assert result.cost_usd == 0.0
    # No assistant model and no override survives -> the requested model.
    assert result.model == "override"


def test_scripted_runner_satisfies_protocol():
    assert isinstance(ScriptedRunner(["x"]), AgentRunner)


def test_runner_maps_sdk_errors_to_error_result(monkeypatch):
    async def boom(*, prompt, options):  # noqa: ANN001
        raise ProcessError("CLI exploded", exit_code=1)
        yield  # makes this an async generator, like the real query

    monkeypatch.setattr(sdk, "query", boom)

    runner = ClaudeAgentRunner()
    result = run(runner.run("prompt"))
    assert result.is_error is True
    assert "ProcessError" in result.text


def test_runner_times_out_to_error_result(monkeypatch):
    async def slow(*, prompt, options):  # noqa: ANN001
        await asyncio.sleep(30)
        yield Assistant([Block("late")])

    monkeypatch.setattr(sdk, "query", slow)

    runner = ClaudeAgentRunner(timeout_seconds=0.01)
    result = run(runner.run("prompt"))
    assert result.is_error is True
    assert "TimeoutError" in result.text


def test_runner_never_swallows_cancellation(monkeypatch):
    async def slow(*, prompt, options):  # noqa: ANN001
        await asyncio.sleep(30)
        yield Assistant([Block("late")])

    monkeypatch.setattr(sdk, "query", slow)

    async def scenario():
        runner = ClaudeAgentRunner()
        task = asyncio.ensure_future(runner.run("prompt"))
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        run(scenario())
