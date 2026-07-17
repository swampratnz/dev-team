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
    ci_fix_question,
    plan_review_question,
    render_plan,
    render_replan,
    replan_review_question,
    review_dispute_question,
    triage_review_question,
    task_failure_question,
)
from dev_team.models import Plan, Task
from dev_team.replan import Replan, ReplanAction


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


def test_question_fail_safe_defaults_to_default():
    # No distinct fail-safe declared: fall back to the default choice.
    assert _question().fail_safe.key == "approve"


def test_question_fail_safe_uses_declared_key():
    assert _question(fail_safe_key="abort").fail_safe.key == "abort"


def test_question_fail_safe_unknown_key_falls_back_to_default():
    # A misdeclared fail-safe key must not raise; degrade to the default.
    assert _question(fail_safe_key="does-not-exist").fail_safe.key == "approve"


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


def test_console_channel_eof_without_fail_safe_uses_default():
    # A question that declares no distinct fail-safe still degrades to its
    # default on EOF (backward-compatible), announced as a fail-safe fallback.
    channel, out = _console([])
    assert channel.ask(_question()).choice == "approve"
    assert "failing safe" in out.getvalue()


def test_console_channel_eof_uses_fail_safe_not_default():
    # A detached (EOF) run must take the safe answer, never the permissive
    # default: here the default is "approve" but the fail-safe is "abort".
    channel, out = _console([])
    reply = channel.ask(_question(fail_safe_key="abort"))
    assert reply.choice == "abort"
    assert "failing safe to 'abort'" in out.getvalue()


def test_console_channel_empty_line_still_takes_default_not_fail_safe():
    # A present human pressing enter picks the default — the fail-safe only
    # applies when input is unavailable, not when a human actually chooses.
    channel, _ = _console([""])
    assert channel.ask(_question(fail_safe_key="abort")).choice == "approve"


def test_console_channel_collects_text_for_choice():
    channel, _ = _console(["revise", "split task 2"])
    reply = channel.ask(_question())
    assert reply == Reply(choice="revise", text="split task 2")


def test_console_channel_eof_during_text_gives_empty():
    channel, _ = _console(["revise"])
    reply = channel.ask(_question())
    assert reply == Reply(choice="revise", text="")


def test_console_channel_broken_pipe_takes_default():
    def broken(prompt):
        raise BrokenPipeError

    out = io.StringIO()
    channel = ConsoleChannel(input_fn=broken, output=out)
    assert channel.ask(_question()).choice == "approve"
    assert "failing safe" in out.getvalue()


def test_console_channel_broken_pipe_during_text_gives_empty():
    answers = iter(["revise"])

    def flaky(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise BrokenPipeError from None

    channel = ConsoleChannel(input_fn=flaky, output=io.StringIO())
    assert channel.ask(_question()) == Reply(choice="revise", text="")


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


def test_channel_approval_gate_question_fails_safe_to_deny():
    channel = ScriptedChannel(script=[Reply(choice="yes")])
    ChannelApprovalGate(channel).review(_approval_request())
    assert channel.questions[0].fail_safe.key == "no"


def test_channel_approval_gate_denies_on_eof():
    # A non-TTY interactive run (piped/CI/nohup) must NOT auto-approve: with no
    # input available the console fails safe to deny.
    console = ConsoleChannel(input_fn=_raise_eof, output=io.StringIO())
    decision = ChannelApprovalGate(console).review(_approval_request())
    assert decision.approved is False


def _raise_eof(prompt):
    raise EOFError


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


def test_plan_review_question_fails_safe_to_abort():
    # Detached (EOF) plan review must abort, not silently start the work.
    assert plan_review_question(_plan(), asked_by="Priya").fail_safe.key == "abort"


def test_task_failure_question_defaults_to_skip():
    question = task_failure_question("T1", "tests: boom", asked_by="Sam")
    assert question.default.key == "skip"
    assert question.find("retry").accepts_text is True
    assert question.context == "tests: boom"
    assert question.asked_by == "Sam"


def test_task_failure_question_fails_safe_to_skip():
    question = task_failure_question("T1", "tests: boom", asked_by="Sam")
    assert question.fail_safe.key == "skip"


def test_render_replan_shows_replacements_with_deps():
    decision = Replan(
        ReplanAction.SPLIT,
        "T2",
        [
            Task(id="T2a", title="part a", description="", acceptance_criteria=["a"]),
            Task(id="T2b", title="part b", description="", acceptance_criteria=["b"],
                 dependencies=["T2a"]),
        ],
        rationale="too coupled",
    )
    text = render_replan(decision)
    assert "split task T2" in text
    assert "Why: too coupled" in text
    assert "T2a: part a" in text
    assert "T2b: part b (after T2a)" in text


def test_render_replan_marks_a_drop_with_no_replacements():
    text = render_replan(Replan(ReplanAction.DROP, "T2"))
    assert "drop task T2" in text
    assert "the failed task is dropped" in text


def test_replan_review_question_defaults_to_apply_and_fails_safe_to_reject():
    question = replan_review_question(
        Replan(ReplanAction.DROP, "T2"), asked_by="Priya"
    )
    # unattended (AutoChannel) applies the manager's autonomous re-plan...
    assert question.default.key == "apply"
    # ...but a detached (EOF) run must not silently rewrite its own plan.
    assert question.fail_safe.key == "reject"
    assert question.find("revise").accepts_text is True
    assert question.asked_by == "Priya"
    assert "T2" in question.prompt


def test_review_dispute_question_defaults_to_overturn_and_fails_safe_to_uphold():
    question = review_dispute_question("T3", context="findings + rationale", asked_by="Sasha")
    # unattended (AutoChannel) applies the judge's autonomous overturn...
    assert question.default.key == "overturn"
    # ...but a detached (EOF) run must not drop a block no human blessed.
    assert question.fail_safe.key == "uphold"
    assert question.asked_by == "Sasha"
    assert "T3" in question.prompt
    assert question.context == "findings + rationale"


def test_triage_review_question_defaults_to_apply_and_fails_safe_to_abort():
    question = triage_review_question("deliver", context="the proposal", asked_by="intake")
    # unattended (AutoChannel) applies the route, matching --intake-apply...
    assert question.default.key == "apply"
    # ...but a detached (EOF) run must not start work no human confirmed.
    assert question.fail_safe.key == "abort"
    assert question.topic == "triage-review"
    assert "deliver" in question.prompt
    assert question.context == "the proposal"


def test_queue_channel_is_importable_from_package_root():
    import dev_team

    assert dev_team.QueueChannel is QueueChannel
    assert isinstance(queue.Queue(), type(QueueChannel().questions))


def test_ci_fix_question_is_autonomous_by_default_and_fails_safe_to_skip():
    q = ci_fix_question(2, ["test (3.12)", "lint"], "boom summary", asked_by="Sam")
    assert q.topic == "ci-fix"
    assert "test (3.12), lint" in q.prompt and "round 2" in q.prompt
    assert q.context == "boom summary"
    assert [c.key for c in q.choices] == ["apply", "skip"]
    # unattended -> fix autonomously; the EOF fail-safe leaves it for a human
    assert AutoChannel().ask(q) == Reply(choice="apply")
    assert q.fail_safe_key == "skip"


def test_ci_fix_question_handles_no_named_failures():
    q = ci_fix_question(1, [], "s", asked_by="Sam")
    assert "the checks" in q.prompt
