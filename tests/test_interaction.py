"""Tests for the interaction channel and its question vocabulary."""

from __future__ import annotations

import io
import queue
import threading

import pytest

from helpers import plan_dict, run

from dev_team.approval import ApprovalRequest
from dev_team.errors import DevTeamError
from dev_team.interaction import (
    AutoChannel,
    ChannelApprovalGate,
    Choice,
    ConsoleChannel,
    InteractionChannel,
    Question,
    QueueChannel,
    Reply,
    ScriptedChannel,
    ask_in_thread,
    plan_review_question,
    render_plan,
    task_failure_question,
)
from dev_team.models import Plan, Task


def _question(**overrides):
    kwargs = dict(
        topic="plan-review",
        prompt="Approve?",
        choices=(
            Choice("approve", "start"),
            Choice("revise", "change", accepts_text=True),
            Choice("abort", "stop"),
        ),
        context="the plan",
    )
    kwargs.update(overrides)
    return Question(**kwargs)


# --- Question ----------------------------------------------------------------


def test_question_requires_choices():
    with pytest.raises(ValueError):
        Question(topic="t", prompt="p", choices=())


def test_question_default_is_first_choice():
    assert _question().default.key == "approve"


def test_question_find_is_case_insensitive():
    assert _question().find("  ABORT ").key == "abort"


def test_question_find_unknown_returns_none():
    assert _question().find("nope") is None


# --- channels ----------------------------------------------------------------


def test_auto_channel_picks_default():
    reply = AutoChannel().ask(_question())
    assert reply == Reply(choice="approve")


def test_channels_satisfy_protocol():
    for channel in (AutoChannel(), ConsoleChannel(), QueueChannel(), ScriptedChannel()):
        assert isinstance(channel, InteractionChannel)


def _console(lines):
    """A ConsoleChannel reading canned lines, capturing output."""

    inputs = list(lines)

    def fake_input(prompt):
        if not inputs:
            raise EOFError
        return inputs.pop(0)

    out = io.StringIO()
    return ConsoleChannel(input_fn=fake_input, output=out), out


def test_console_channel_returns_choice():
    channel, out = _console(["abort"])
    reply = channel.ask(_question())
    assert reply == Reply(choice="abort")
    assert "Approve?" in out.getvalue()
    assert "the plan" in out.getvalue()


def test_console_channel_skips_empty_context():
    channel, out = _console(["approve"])
    channel.ask(_question(context=""))
    assert "Approve?" in out.getvalue()


def test_console_channel_empty_input_takes_default():
    channel, _ = _console([""])
    assert channel.ask(_question()).choice == "approve"


def test_console_channel_reprompts_on_unknown():
    channel, out = _console(["what", "approve"])
    assert channel.ask(_question()).choice == "approve"
    assert "unrecognised" in out.getvalue()


def test_console_channel_eof_takes_default():
    channel, out = _console([])
    assert channel.ask(_question()).choice == "approve"
    assert "defaulting" in out.getvalue()


def test_console_channel_collects_text_for_choice():
    channel, _ = _console(["revise", "split task 2"])
    reply = channel.ask(_question())
    assert reply == Reply(choice="revise", text="split task 2")


def test_console_channel_eof_during_text_gives_empty():
    channel, _ = _console(["revise"])
    reply = channel.ask(_question())
    assert reply == Reply(choice="revise", text="")


def test_console_channel_writes_to_stderr_by_default(capsys):
    channel = ConsoleChannel(input_fn=lambda prompt: "approve")
    channel.ask(_question())
    captured = capsys.readouterr()
    assert "Approve?" in captured.err
    assert captured.out == ""


def test_queue_channel_round_trip():
    channel = QueueChannel()

    def service():
        question = channel.questions.get(timeout=5)
        channel.replies.put(Reply(choice="abort", text=question.topic))

    thread = threading.Thread(target=service)
    thread.start()
    reply = channel.ask(_question())
    thread.join()
    assert reply == Reply(choice="abort", text="plan-review")


def test_queue_channel_timeout_takes_default():
    channel = QueueChannel(timeout=0.01)
    assert channel.ask(_question()).choice == "approve"


def test_scripted_channel_replays_and_records():
    channel = ScriptedChannel(script=[Reply(choice="approve")])
    assert channel.ask(_question()).choice == "approve"
    assert channel.questions[0].topic == "plan-review"


def test_scripted_channel_exhausted_raises():
    channel = ScriptedChannel()
    with pytest.raises(DevTeamError):
        channel.ask(_question())


def test_ask_in_thread_runs_channel():
    reply = run(ask_in_thread(AutoChannel(), _question()))
    assert reply.choice == "approve"


# --- ChannelApprovalGate -------------------------------------------------------


def _approval_request():
    return ApprovalRequest(action="commit feature: Login", detail="2 task(s)", risk="medium")


def test_channel_approval_gate_approves():
    gate = ChannelApprovalGate(ScriptedChannel(script=[Reply(choice="yes")]))
    decision = gate.review(_approval_request())
    assert decision.approved is True
    assert "interactively" in decision.reason


def test_channel_approval_gate_denies_with_reason():
    gate = ChannelApprovalGate(
        ScriptedChannel(script=[Reply(choice="no", text="not yet")])
    )
    decision = gate.review(_approval_request())
    assert decision.approved is False
    assert decision.reason == "not yet"


def test_channel_approval_gate_denies_default_reason():
    gate = ChannelApprovalGate(ScriptedChannel(script=[Reply(choice="no")]))
    decision = gate.review(_approval_request())
    assert decision.approved is False
    assert decision.reason == "denied interactively"


def test_channel_approval_gate_question_carries_risk():
    channel = ScriptedChannel(script=[Reply(choice="yes")])
    ChannelApprovalGate(channel).review(_approval_request())
    question = channel.questions[0]
    assert question.topic == "approval"
    assert "risk: medium" in question.context


# --- question builders ----------------------------------------------------------


def _plan():
    data = plan_dict(2)
    tasks = [Task(**t) for t in data["tasks"]]
    tasks[1].dependencies = ["T1"]
    return Plan(summary=data["summary"], tasks=tasks)


def test_render_plan_shows_tasks_criteria_and_deps():
    text = render_plan(_plan())
    assert "Plan: the plan" in text
    assert "T1: Task 1" in text
    assert "- it works" in text
    assert "T2: Task 2 (after T1)" in text


def test_plan_review_question_defaults_to_approve():
    question = plan_review_question(_plan(), asked_by="Priya")
    assert question.default.key == "approve"
    assert question.asked_by == "Priya"
    assert question.find("revise").accepts_text is True
    assert "2 task(s)" in question.prompt


def test_task_failure_question_defaults_to_skip():
    question = task_failure_question("T1", "tests: boom", asked_by="Sam")
    assert question.default.key == "skip"
    assert question.find("retry").accepts_text is True
    assert question.context == "tests: boom"
    assert question.asked_by == "Sam"


def test_queue_channel_is_importable_from_package_root():
    import dev_team

    assert dev_team.QueueChannel is QueueChannel
    assert isinstance(queue.Queue(), type(QueueChannel().questions))
