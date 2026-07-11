"""Tests for the conversational chat mode."""

from __future__ import annotations

import io
import json

import pytest

from helpers import run

from dev_team.chat import (
    BRIEF_PROMPT,
    ChatSession,
    _request_from_brief,
    chat_system_prompt,
)
from dev_team.errors import JSONExtractionError
from dev_team.models import FeatureRequest
from dev_team.persona import Persona
from dev_team.sdk import ClaudeChatBackend, _default_client_factory


# --- prompt & brief parsing ---------------------------------------------------


def test_chat_system_prompt_includes_persona():
    prompt = chat_system_prompt(Persona(name="Priya", role="product-manager"))
    assert prompt.startswith("Your name is Priya.")
    assert '{"title"' in prompt


def test_chat_system_prompt_without_persona():
    prompt = chat_system_prompt(None)
    assert prompt.startswith("You are the product manager")


def test_request_from_brief_happy():
    request = _request_from_brief(
        {"title": " Login ", "description": " Add login ", "constraints": ["fast", 3, " "]}
    )
    assert request == FeatureRequest(
        title="Login", description="Add login", constraints=["fast"]
    )


def test_request_from_brief_constraints_not_a_list():
    request = _request_from_brief(
        {"title": "Login", "description": "Add login", "constraints": "fast"}
    )
    assert request.constraints == []


@pytest.mark.parametrize(
    "data",
    [
        {"description": "d"},
        {"title": " ", "description": "d"},
        {"title": "t"},
        {"title": "t", "description": ""},
    ],
)
def test_request_from_brief_rejects_incomplete(data):
    with pytest.raises(JSONExtractionError):
        _request_from_brief(data)


# --- ChatSession ---------------------------------------------------------------


class FakeBackend:
    """A ChatBackend replaying canned replies; brief prompts get the brief."""

    def __init__(self, replies=(), brief=None):
        self.replies = list(replies)
        self.brief = brief if brief is not None else json.dumps(
            {"title": "Login", "description": "Add login", "constraints": []}
        )
        self.sent = []
        self.closed = False

    async def send(self, text):
        self.sent.append(text)
        if text == BRIEF_PROMPT:
            return self.brief
        return self.replies.pop(0) if self.replies else "tell me more"

    async def close(self):
        self.closed = True


def _session(lines, backend=None, run_result=0):
    inputs = list(lines)

    def fake_input(prompt):
        if not inputs:
            raise EOFError
        return inputs.pop(0)

    backend = backend or FakeBackend()
    runs = []

    async def run_feature(request, deliver):
        runs.append((request, deliver))
        return run_result

    out = io.StringIO()
    session = ChatSession(
        backend=backend,
        run_feature=run_feature,
        pm_name="Priya",
        input_fn=fake_input,
        output=out,
    )
    return session, backend, runs, out


def test_chat_quit_immediately():
    session, backend, runs, out = _session(["/quit"])
    assert run(session.run()) == 0
    assert runs == []
    assert backend.closed is True
    assert "chatting with Priya" in out.getvalue()


def test_chat_eof_quits():
    session, backend, _, _ = _session([])
    assert run(session.run()) == 0
    assert backend.closed is True


def test_chat_free_text_gets_reply():
    session, backend, _, out = _session(["I want login", "/quit"], FakeBackend(["what kind?"]))
    run(session.run())
    assert backend.sent[0] == "I want login"
    assert "Priya > what kind?" in out.getvalue()


def test_chat_blank_lines_are_ignored():
    session, backend, _, _ = _session(["   ", "/quit"])
    run(session.run())
    assert backend.sent == []


def test_chat_help_and_unknown_command():
    session, backend, _, out = _session(["/help", "/wat", "/quit"])
    run(session.run())
    assert backend.sent == []  # commands never go to the model
    assert "/deliver" in out.getvalue()
    assert "unknown command '/wat'" in out.getvalue()


def test_chat_run_hands_off_simulation():
    session, backend, runs, out = _session(["/run", "/quit"])
    assert run(session.run()) == 0
    assert runs == [(FeatureRequest(title="Login", description="Add login"), False)]
    assert "handing off to the team (simulation)" in out.getvalue()
    assert "run succeeded" in out.getvalue()


def test_chat_deliver_hands_off_delivery():
    session, _, runs, out = _session(["/deliver", "/quit"])
    run(session.run())
    assert runs[0][1] is True
    assert "handing off to the team (delivery)" in out.getvalue()


def test_chat_failed_run_exit_code_survives_quit():
    session, _, runs, out = _session(["/run", "/quit"], run_result=1)
    assert run(session.run()) == 1
    assert "finished with issues" in out.getvalue()


def test_chat_bad_brief_keeps_chatting():
    backend = FakeBackend(brief="no json here")
    session, _, runs, out = _session(["/run", "/quit"], backend)
    assert run(session.run()) == 0
    assert runs == []
    assert "could not distil a brief" in out.getvalue()


def test_chat_brief_not_an_object_keeps_chatting():
    backend = FakeBackend(brief="[1, 2]")
    session, _, runs, out = _session(["/run", "/quit"], backend)
    run(session.run())
    assert runs == []
    assert "could not distil a brief" in out.getvalue()


def test_chat_writes_to_stdout_by_default(capsys):
    session, _, _, _ = _session(["/quit"])
    session.output = None
    run(session.run())
    assert "chatting with Priya" in capsys.readouterr().out


# --- ClaudeChatBackend -----------------------------------------------------------


class _Block:
    def __init__(self, text):
        self.text = text


class _Message:
    def __init__(self, texts):
        self.content = [_Block(t) for t in texts]


class FakeClient:
    instances = []

    def __init__(self, options):
        self.options = options
        self.connected = False
        self.queries = []
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        yield _Message(["hello", "there"])

    async def disconnect(self):
        self.connected = False


def test_claude_chat_backend_holds_one_session():
    FakeClient.instances = []
    backend = ClaudeChatBackend(
        system_prompt="You are Priya.", client_factory=FakeClient
    )
    reply = run(backend.send("hi"))
    assert reply == "hello\nthere"
    reply2 = run(backend.send("again"))
    assert reply2 == "hello\nthere"
    assert len(FakeClient.instances) == 1  # one persistent session, two queries
    client = FakeClient.instances[0]
    assert client.queries == ["hi", "again"]
    assert client.options.system_prompt == "You are Priya."
    assert client.options.allowed_tools == []


def test_claude_chat_backend_sets_model_when_given():
    FakeClient.instances = []
    backend = ClaudeChatBackend(
        system_prompt="s", model="claude-x", client_factory=FakeClient
    )
    run(backend.send("hi"))
    assert FakeClient.instances[0].options.model == "claude-x"


def test_claude_chat_backend_close_disconnects_and_resets():
    FakeClient.instances = []
    backend = ClaudeChatBackend(system_prompt="s", client_factory=FakeClient)
    run(backend.send("hi"))
    client = FakeClient.instances[0]
    run(backend.close())
    assert client.connected is False
    assert backend._client is None


def test_claude_chat_backend_close_without_session_is_noop():
    backend = ClaudeChatBackend(system_prompt="s", client_factory=FakeClient)
    run(backend.close())  # nothing to disconnect; must not raise


def test_default_client_factory_builds_real_client_lazily():
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    client = _default_client_factory(ClaudeAgentOptions())
    # constructing the client is safe — its subprocess only starts on connect()
    assert isinstance(client, ClaudeSDKClient)
