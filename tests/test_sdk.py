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


# --- ClaudeAgentSession / FakeAgentSession ------------------------------


class _SessionClient:
    """A fake persistent SDK client: connect / query / receive_response / disconnect."""

    instances = []

    def __init__(self, options):
        self.options = options
        self.connected = False
        self.disconnected = False
        self.queries = []
        _SessionClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        yield Assistant([Block("did it")], model="claude-real")
        yield ResultMsg(total_cost_usd=0.3, num_turns=2, is_error=False, result="summary")

    async def disconnect(self):
        self.disconnected = True


def test_session_reuses_one_client_across_turns():
    from dev_team.sdk import ClaudeAgentSession

    _SessionClient.instances = []
    session = ClaudeAgentSession(
        system_prompt="be an engineer",
        allowed_tools=["Read", "Write"],
        model="claude-x",
        cwd="/work",
        client_factory=_SessionClient,
    )
    r1 = run(session.send("attempt 1"))
    r2 = run(session.send("address the feedback"))
    assert r1.text == "did it\nsummary"
    assert r1.cost_usd == 0.3 and r1.num_turns == 2 and r1.model == "claude-real"
    assert r2.text == "did it\nsummary"
    assert len(_SessionClient.instances) == 1  # one persistent client, two turns
    client = _SessionClient.instances[0]
    assert client.queries == ["attempt 1", "address the feedback"]
    assert client.options.system_prompt == "be an engineer"
    assert client.options.allowed_tools == ["Read", "Write"]
    assert client.options.model == "claude-x"
    assert client.options.cwd == "/work"


def test_session_aclose_disconnects_and_resets():
    from dev_team.sdk import ClaudeAgentSession

    _SessionClient.instances = []
    session = ClaudeAgentSession(client_factory=_SessionClient)
    run(session.send("hi"))
    client = _SessionClient.instances[0]
    run(session.aclose())
    assert client.disconnected is True
    assert session._client is None


def test_session_maps_sdk_errors_to_error_result():
    from dev_team.sdk import ClaudeAgentSession

    class BoomClient:
        def __init__(self, options):
            pass

        async def connect(self):
            pass

        async def query(self, text):
            raise ProcessError("CLI exploded", exit_code=1)

        async def receive_response(self):
            yield Assistant([Block("unreached")])

        async def disconnect(self):
            pass

    result = run(ClaudeAgentSession(client_factory=BoomClient).send("x"))
    assert result.is_error is True
    assert "ProcessError" in result.text


def test_session_times_out_to_error_result():
    from dev_team.sdk import ClaudeAgentSession

    class SlowClient:
        def __init__(self, options):
            pass

        async def connect(self):
            pass

        async def query(self, text):
            pass

        async def receive_response(self):
            await asyncio.sleep(30)
            yield Assistant([Block("late")])

        async def disconnect(self):
            pass

    result = run(ClaudeAgentSession(client_factory=SlowClient, timeout_seconds=0.01).send("x"))
    assert result.is_error is True
    assert "TimeoutError" in result.text


def test_session_timeout_also_covers_a_hanging_query():
    from dev_team.sdk import ClaudeAgentSession

    # The timeout wraps the whole turn, so a hang in query() (not just the
    # response stream) also surfaces as an error result.
    class HangingQueryClient:
        def __init__(self, options):
            pass

        async def connect(self):
            pass

        async def query(self, text):
            await asyncio.sleep(30)

        async def receive_response(self):
            yield Assistant([Block("unreached")])

        async def disconnect(self):
            pass

    result = run(
        ClaudeAgentSession(client_factory=HangingQueryClient, timeout_seconds=0.01).send("x")
    )
    assert result.is_error is True
    assert "TimeoutError" in result.text


def test_fake_agent_session_records_prompts_and_repeats_last_result():
    from dev_team.sdk import FakeAgentSession

    session = FakeAgentSession(results=[AgentResult(text="a"), AgentResult(text="b")])
    assert run(session.send("p1")).text == "a"
    assert run(session.send("p2")).text == "b"
    assert run(session.send("p3")).text == "b"  # last result repeats
    assert session.prompts == ["p1", "p2", "p3"]
    run(session.aclose())
    assert session.closed is True


def test_fake_agent_session_defaults_to_empty_success():
    from dev_team.sdk import FakeAgentSession

    result = run(FakeAgentSession().send("p"))
    assert result.text == "" and result.is_error is False


def test_session_aclose_without_a_client_is_a_noop():
    from dev_team.sdk import ClaudeAgentSession

    # aclose before any send: no client was ever opened, so it just returns.
    run(ClaudeAgentSession(client_factory=_SessionClient).aclose())
