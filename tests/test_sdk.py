"""Tests for the Claude Agent SDK adapter."""

from __future__ import annotations

import asyncio
import os

import pytest
from claude_agent_sdk import ProcessError
from helpers import run

from dev_team import sdk
from dev_team.sdk import (
    AgentResult,
    AgentRunner,
    ClaudeAgentRunner,
    SessionAgentRunner,
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


# --- SessionAgentRunner --------------------------------------------------


class _SessionBlock:
    def __init__(self, text):
        self.text = text


class _SessionMessage:
    """A minimal message: text content only, no cost/model metadata."""

    def __init__(self, texts):
        self.content = [_SessionBlock(t) for t in texts]


class _SessionResultMessage:
    """A message carrying the cost/model/result metadata a real SDK result has."""

    def __init__(
        self,
        texts,
        *,
        model=None,
        total_cost_usd=None,
        num_turns=0,
        is_error=False,
        result=None,
    ):
        self.content = [_SessionBlock(t) for t in texts]
        self.model = model
        self.total_cost_usd = total_cost_usd
        self.num_turns = num_turns
        self.is_error = is_error
        self.result = result


def _session_client_factory(responses):
    """A fake ``ClaudeSDKClient`` factory; ``responses`` is popped per query().

    An item that is an exception instance is raised from ``receive_response``
    instead of yielded, simulating a dropped connection.
    """

    instances = []

    class _Client:
        def __init__(self, options):
            self.options = options
            self.connected = False
            self.queries = []
            instances.append(self)

        async def connect(self):
            self.connected = True

        async def query(self, text):
            self.queries.append(text)

        async def receive_response(self):
            item = responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            yield _SessionMessage([item])

        async def disconnect(self):
            self.connected = False

    _Client.instances = instances
    return _Client


def test_session_agent_runner_satisfies_protocol():
    assert isinstance(SessionAgentRunner(), AgentRunner)


def test_session_runner_reuses_client_for_same_key():
    client_cls = _session_client_factory(["one", "two"])
    runner = SessionAgentRunner(client_factory=client_cls)

    first = run(runner.run("hi", task_key="T1"))
    second = run(runner.run("again", task_key="T1"))

    assert first.text == "one"
    assert second.text == "two"
    assert len(client_cls.instances) == 1  # same client instance, two turns
    assert client_cls.instances[0].queries == ["hi", "again"]


def test_session_runner_isolates_different_keys():
    client_cls = _session_client_factory(["a", "b"])
    runner = SessionAgentRunner(client_factory=client_cls)

    run(runner.run("hi", task_key="T1"))
    run(runner.run("hi2", task_key="T2"))

    assert len(client_cls.instances) == 2
    assert client_cls.instances[0].queries == ["hi"]
    assert client_cls.instances[1].queries == ["hi2"]


def test_session_runner_close_disconnects_and_a_later_call_reconnects():
    client_cls = _session_client_factory(["a", "b"])
    runner = SessionAgentRunner(client_factory=client_cls)

    run(runner.run("hi", task_key="T1"))
    first_client = client_cls.instances[0]
    run(runner.close("T1"))
    assert first_client.connected is False

    run(runner.run("again", task_key="T1"))
    assert len(client_cls.instances) == 2
    assert client_cls.instances[1] is not first_client


def test_session_runner_close_without_a_session_is_a_noop():
    runner = SessionAgentRunner(client_factory=_session_client_factory([]))
    run(runner.close("never-opened"))  # must not raise


def test_session_runner_falls_back_on_dropped_continuation():
    client_cls = _session_client_factory(
        ["first", ProcessError("dropped", exit_code=1), "recovered"]
    )
    runner = SessionAgentRunner(client_factory=client_cls)

    first = run(runner.run("full context", task_key="T1"))
    second = run(runner.run("just feedback", task_key="T1"))

    assert first.text == "first"
    assert second.text == "recovered"
    assert second.is_error is False
    assert len(client_cls.instances) == 2  # stale client discarded, fresh one used
    assert client_cls.instances[0].connected is False
    # the fresh session replayed the FULL history, not just this turn's
    # compact prompt — the whole point of session continuity is that the
    # caller may have sent only the feedback text this turn.
    assert client_cls.instances[1].queries == ["full context\n\njust feedback"]


def test_session_runner_reconnects_on_model_mismatch():
    client_cls = _session_client_factory(["first", "escalated"])
    runner = SessionAgentRunner(client_factory=client_cls)

    run(runner.run("full context", task_key="T1", model="base-model"))
    second = run(
        runner.run("just feedback", task_key="T1", model="strong-model")
    )

    assert second.text == "escalated"
    assert len(client_cls.instances) == 2
    # the stale (wrong-model) client was discarded, never queried again
    assert client_cls.instances[0].connected is False
    assert client_cls.instances[0].queries == ["full context"]
    # the new client was connected with the escalated model and carries
    # full context (attempt 1's prompt + this turn's), not just this turn's
    assert client_cls.instances[1].options.model == "strong-model"
    assert client_cls.instances[1].queries == ["full context\n\njust feedback"]


def test_session_runner_without_task_key_is_one_shot():
    client_cls = _session_client_factory(["solo"])
    runner = SessionAgentRunner(client_factory=client_cls)

    result = run(runner.run("hi"))

    assert result.text == "solo"
    assert len(client_cls.instances) == 1
    assert client_cls.instances[0].connected is False  # disconnected after use


def test_session_runner_first_call_error_becomes_error_result():
    client_cls = _session_client_factory([ProcessError("boom", exit_code=1)])
    runner = SessionAgentRunner(client_factory=client_cls)

    result = run(runner.run("hi", task_key="T1"))

    assert result.is_error is True
    assert "ProcessError" in result.text


def test_session_runner_times_out_to_error_result():
    class _SlowClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def query(self, text):
            await asyncio.sleep(30)

        async def receive_response(self):
            if False:  # pragma: no cover - never reached; makes this a generator
                yield None

        async def disconnect(self):
            pass

    runner = SessionAgentRunner(client_factory=_SlowClient, timeout_seconds=0.01)
    result = run(runner.run("hi", task_key="T1"))
    assert result.is_error is True
    assert "TimeoutError" in result.text


def test_session_runner_connects_with_expected_options():
    client_cls = _session_client_factory(["x"])
    runner = SessionAgentRunner(client_factory=client_cls)

    run(
        runner.run(
            "hi",
            task_key="T1",
            system_prompt="sys",
            allowed_tools=["Read"],
            model="m",
            cwd="/wd",
        )
    )

    options = client_cls.instances[0].options
    assert options.system_prompt == "sys"
    assert options.allowed_tools == ["Read"]
    assert options.model == "m"
    assert options.cwd == "/wd"
    assert options.permission_mode == "acceptEdits"


def test_session_runner_captures_cost_and_model_metadata():
    class _MetaClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def query(self, text):
            pass

        async def receive_response(self):
            yield _SessionResultMessage(["hi"], model="claude-real")
            yield _SessionResultMessage(
                [],
                total_cost_usd=0.25,
                num_turns=2,
                is_error=False,
                result="done",
            )

        async def disconnect(self):
            pass

    runner = SessionAgentRunner(client_factory=_MetaClient)
    result = run(runner.run("prompt", task_key="T1"))

    assert result.text == "hi\ndone"
    assert result.cost_usd == 0.25
    assert result.num_turns == 2
    assert result.model == "claude-real"
    assert result.is_error is False


def test_session_runner_needs_no_special_credentials(monkeypatch):
    # Same construction path as the already-accepted ClaudeChatBackend/
    # ClaudeSDKClient: no env var is read, so an empty environment works fine
    # as long as a client factory is supplied (as the real one is, in prod).
    monkeypatch.setattr(os, "environ", {})
    client_cls = _session_client_factory(["ok"])
    runner = SessionAgentRunner(client_factory=client_cls)

    result = run(runner.run("hi", task_key="T1"))

    assert result.text == "ok"
