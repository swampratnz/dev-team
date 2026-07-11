"""Interactive control: a channel through which a run consults its human.

The engine and workflow are autonomous by default, but a run becomes
*collaborative* when an :class:`InteractionChannel` is supplied: the plan is
presented for approval before any work starts, a task that exhausts its
attempts escalates to the human instead of silently failing, and the feature
commit asks for a go-ahead.

The channel interface is deliberately synchronous — a human answering on
stdin, a queue serviced by a UI thread, or an auto-responder are all blocking
operations from the run's point of view. Async callers hop through
``asyncio.to_thread`` (see :func:`ask_in_thread`) so the event loop stays free
while a question is pending. UIs (web, TUI, chat-ops) integrate by servicing a
:class:`QueueChannel` from their own event loop; nothing in the engine knows
or cares what is on the other end.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence, TextIO, runtime_checkable

from .approval import ApprovalDecision, ApprovalRequest
from .errors import DevTeamError
from .models import Plan


@dataclass(frozen=True)
class Choice:
    """One selectable answer to a :class:`Question`.

    Attributes:
        key: The short token the human types (e.g. ``"approve"``).
        label: One-line description shown next to the key.
        accepts_text: Whether this choice carries free text (e.g. revision
            feedback), prompted for after the choice is made.
    """

    key: str
    label: str
    accepts_text: bool = False


@dataclass(frozen=True)
class Question:
    """A decision the run needs from its human.

    Attributes:
        topic: Machine-friendly kind: ``plan-review``, ``task-failure``, or
            ``approval``. UIs may switch on it; humans see ``prompt``.
        prompt: The one-line question being asked.
        choices: The selectable answers, first is the default.
        context: Supporting evidence (a rendered plan, failure output),
            shown before the prompt.
        asked_by: Persona/role name of the agent surfacing the question.
    """

    topic: str
    prompt: str
    choices: Sequence[Choice]
    context: str = ""
    asked_by: str = "workflow"

    def __post_init__(self) -> None:
        if not self.choices:
            raise ValueError("a question needs at least one choice")

    @property
    def default(self) -> Choice:
        """The choice taken by non-interactive channels."""

        return self.choices[0]

    def find(self, key: str) -> Optional[Choice]:
        """The choice matching ``key`` (case-insensitive), if any."""

        lowered = key.strip().lower()
        for choice in self.choices:
            if choice.key == lowered:
                return choice
        return None


@dataclass(frozen=True)
class Reply:
    """The human's answer to a :class:`Question`."""

    choice: str
    text: str = ""


@runtime_checkable
class InteractionChannel(Protocol):
    """Anything that can put a :class:`Question` to a human and wait."""

    def ask(self, question: Question) -> Reply:
        """Block until the question is answered; return the :class:`Reply`."""
        ...


class AutoChannel:
    """Answers every question with its default choice — unattended runs."""

    def ask(self, question: Question) -> Reply:
        return Reply(choice=question.default.key)


@dataclass
class ConsoleChannel:
    """Prompts on a terminal: context and question out, one line back in.

    Concurrent questioners (parallel task workers) are serialised by a lock so
    two prompts never interleave on the terminal. Unknown input re-prompts;
    EOF (stdin closed) falls back to the default choice so a detached run
    degrades to autonomous instead of crashing.
    """

    input_fn: Callable[[str], str] = input
    output: Optional[TextIO] = None  # defaults to stderr at call time

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def _write(self, text: str) -> None:
        stream = self.output if self.output is not None else sys.stderr
        stream.write(text + "\n")
        stream.flush()

    def ask(self, question: Question) -> Reply:
        with self._lock:
            self._write("")
            if question.context:
                self._write(question.context)
            self._write(f"{question.asked_by} asks: {question.prompt}")
            menu = "  ".join(
                f"[{c.key}] {c.label}" for c in question.choices
            )
            while True:
                try:
                    raw = self.input_fn(f"{menu} > ")
                except EOFError:
                    self._write(
                        f"(no input available; defaulting to '{question.default.key}')"
                    )
                    return Reply(choice=question.default.key)
                if not raw.strip():
                    choice = question.default
                else:
                    choice = question.find(raw)
                if choice is None:
                    self._write(f"unrecognised choice: {raw.strip()!r}")
                    continue
                text = ""
                if choice.accepts_text:
                    try:
                        text = self.input_fn(f"{choice.label} — details: ").strip()
                    except EOFError:
                        text = ""
                return Reply(choice=choice.key, text=text)


@dataclass
class QueueChannel:
    """A channel serviced by another thread (a UI, a bot, a test).

    :meth:`ask` publishes the question on :attr:`questions` and blocks until
    the servicer puts a :class:`Reply` on :attr:`replies`. A ``timeout``
    protects the run from a servicer that died: on expiry the question's
    default choice is taken.
    """

    timeout: Optional[float] = None
    questions: "queue.Queue[Question]" = field(default_factory=queue.Queue)
    replies: "queue.Queue[Reply]" = field(default_factory=queue.Queue)

    def ask(self, question: Question) -> Reply:
        self.questions.put(question)
        try:
            return self.replies.get(timeout=self.timeout)
        except queue.Empty:
            return Reply(choice=question.default.key)


@dataclass
class ScriptedChannel:
    """Replays canned replies in order — the test double for interactivity."""

    script: Sequence[Reply] = ()

    def __post_init__(self) -> None:
        self._pending = list(self.script)
        self.questions: list[Question] = []

    def ask(self, question: Question) -> Reply:
        self.questions.append(question)
        if not self._pending:
            raise DevTeamError(
                f"ScriptedChannel ran out of replies (asked: {question.prompt!r})"
            )
        return self._pending.pop(0)


@dataclass
class ChannelApprovalGate:
    """An :class:`~dev_team.approval.ApprovalGate` that asks the channel.

    Bridges the engine's yes/no approval points (feature commit, guarded
    commands) into the interactive conversation.
    """

    channel: InteractionChannel

    def review(self, request: ApprovalRequest) -> ApprovalDecision:
        question = Question(
            topic="approval",
            prompt=f"Approve: {request.action}?",
            choices=(
                Choice("yes", "approve"),
                Choice("no", "deny", accepts_text=True),
            ),
            context=f"risk: {request.risk} — {request.detail}",
        )
        reply = self.channel.ask(question)
        if reply.choice == "yes":
            return ApprovalDecision(approved=True, reason="approved interactively")
        reason = reply.text or "denied interactively"
        return ApprovalDecision(approved=False, reason=reason)


async def ask_in_thread(channel: InteractionChannel, question: Question) -> Reply:
    """Await a blocking :meth:`InteractionChannel.ask` off the event loop."""

    return await asyncio.to_thread(channel.ask, question)


def render_plan(plan: Plan) -> str:
    """A human-readable rendering of a plan for review."""

    lines = [f"Plan: {plan.summary}"]
    for task in plan.tasks:
        deps = f" (after {', '.join(task.dependencies)})" if task.dependencies else ""
        lines.append(f"  {task.id}: {task.title}{deps}")
        for criterion in task.acceptance_criteria:
            lines.append(f"    - {criterion}")
    return "\n".join(lines)


def plan_review_question(plan: Plan, *, asked_by: str) -> Question:
    """The plan-approval question. Default (unattended) answer: approve."""

    return Question(
        topic="plan-review",
        prompt=f"Approve this plan ({len(plan.tasks)} task(s))?",
        choices=(
            Choice("approve", "start the work"),
            Choice("revise", "request changes", accepts_text=True),
            Choice("abort", "stop the run"),
        ),
        context=render_plan(plan),
        asked_by=asked_by,
    )


def task_failure_question(
    task_id: str, evidence: str, *, asked_by: str
) -> Question:
    """The failed-task escalation. Default (unattended) answer: skip.

    ``skip`` preserves the autonomous behaviour — the task stays failed and
    the run carries on; ``retry`` grants a fresh round of attempts with the
    human's guidance folded into the engineer's feedback.
    """

    return Question(
        topic="task-failure",
        prompt=f"Task {task_id} failed all attempts. What now?",
        choices=(
            Choice("skip", "accept the failure and continue"),
            Choice("retry", "retry with your guidance", accepts_text=True),
        ),
        context=evidence,
        asked_by=asked_by,
    )
