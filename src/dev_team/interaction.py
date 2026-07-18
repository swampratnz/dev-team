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
from .replan import Replan


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
        fail_safe_key: Key of the choice a channel must take when input is
            *unavailable* (a closed/non-TTY stdin, a dead servicer) — distinct
            from :attr:`default`, which a present human selects by pressing
            enter. Left ``None`` for questions where failing closed and the
            interactive default coincide.
    """

    topic: str
    prompt: str
    choices: Sequence[Choice]
    context: str = ""
    asked_by: str = "workflow"
    fail_safe_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.choices:
            raise ValueError("a question needs at least one choice")

    @property
    def default(self) -> Choice:
        """The choice taken by non-interactive channels."""

        return self.choices[0]

    @property
    def fail_safe(self) -> Choice:
        """The choice to take when no human input is available (EOF).

        A detached interactive run (piped stdin, CI, ``nohup``) must fail
        *closed* — deny an approval, abort a plan review — rather than fall
        through to the convenience :attr:`default`, which for an approval is
        "yes" and would auto-approve the very commits and risky commands the
        interactive mode exists to gate. When :attr:`fail_safe_key` is unset,
        or names no known choice, the default is used unchanged so questions
        that need no distinct safe answer are unaffected.
        """

        if self.fail_safe_key is None:
            return self.default
        return self.find(self.fail_safe_key) or self.default

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
    EOF (stdin closed — a piped/CI/``nohup`` run that was nonetheless handed an
    interactive channel) falls back to the question's *fail-safe* choice, not
    its default, so a detached run degrades to the safe answer (deny, abort)
    instead of auto-approving or crashing.
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
                except (EOFError, OSError):
                    # EOF or a closed stream: no human is on the other end, so
                    # fail closed to the question's safe answer rather than
                    # crash — or, worse, take a permissive default that would
                    # auto-approve unattended.
                    safe = question.fail_safe
                    self._write(
                        f"(no input available; failing safe to '{safe.key}')"
                    )
                    return Reply(choice=safe.key)
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
                    except (EOFError, OSError):
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
            # No input available (detached stdin) must deny, never approve.
            fail_safe_key="no",
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
    """The plan-approval question.

    Default (unattended :class:`AutoChannel`) answer: approve. But when an
    interactive channel finds no input available (EOF), it fails safe to
    ``abort`` — a detached run must not silently start work no human blessed.
    """

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
        fail_safe_key="abort",
    )


def task_failure_question(
    task_id: str, evidence: str, *, asked_by: str
) -> Question:
    """The failed-task escalation. Default (unattended) answer: skip.

    ``skip`` preserves the autonomous behaviour — the task stays failed and
    the run carries on; ``retry`` grants a fresh round of attempts with the
    human's guidance folded into the engineer's feedback. With no input
    available (EOF) the fail-safe is ``skip``: retrying blind would burn
    attempts without the guidance ``retry`` exists to carry.
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
        fail_safe_key="skip",
    )


def render_replan(decision: Replan) -> str:
    """A human-readable rendering of a proposed re-plan for supervision."""

    lines = [f"Proposed: {decision.action.value} task {decision.failed_task_id}"]
    if decision.rationale:
        lines.append(f"Why: {decision.rationale}")
    if decision.replacements:
        lines.append("Replacement tasks:")
        for task in decision.replacements:
            deps = f" (after {', '.join(task.dependencies)})" if task.dependencies else ""
            lines.append(f"  {task.id}: {task.title}{deps}")
    else:
        lines.append("(no replacement tasks — the failed task is dropped)")
    return "\n".join(lines)


def ci_fix_question(
    round_num: int, failed: Sequence[str], summary: str, *, asked_by: str
) -> Question:
    """Supervise one round of autonomous CI-fix-and-repush on an open PR.

    Default (unattended :class:`AutoChannel`) answer: apply — the team fixes and
    re-pushes autonomously when no human is watching. With no input available
    (EOF) the fail-safe is ``skip``: a detached run must not force-push a fix
    to an open PR that no human blessed.
    """

    checks = ", ".join(failed) or "the checks"
    return Question(
        topic="ci-fix",
        prompt=f"CI is failing ({checks}). Fix it and re-push (round {round_num})?",
        choices=(
            Choice("apply", "let the engineer fix it and force-push to the PR branch"),
            Choice("skip", "leave the PR's CI failure for a human"),
        ),
        context=summary,
        asked_by=asked_by,
        fail_safe_key="skip",
    )


def review_dispute_question(task_id: str, *, context: str, asked_by: str) -> Question:
    """Supervise a debate whose judge would overturn a blocking review.

    Default (unattended :class:`AutoChannel`) answer: overturn — matching the
    autonomous path, where the judge's ruling is applied when no human is
    watching. With no input available (EOF) the fail-safe is ``uphold``: a
    detached run must not drop a reviewer's block that no human blessed.
    """

    return Question(
        topic="review-dispute",
        prompt=f"The review debate would overturn the block on {task_id}. Accept it?",
        choices=(
            Choice("overturn", "accept the overturn and let the change proceed"),
            Choice("uphold", "keep the changes-requested verdict"),
        ),
        context=context,
        asked_by=asked_by,
        fail_safe_key="uphold",
    )


def triage_review_question(route: str, *, context: str, asked_by: str) -> Question:
    """Confirm applying an intake triage decision (``--intake --interactive``).

    Default (unattended :class:`AutoChannel`) answer: apply — matching
    ``--intake-apply``'s autonomous posture. With no input available (EOF) the
    fail-safe is ``abort``: a detached run must not start work (and spend) on a
    route no human confirmed.
    """

    return Question(
        topic="triage-review",
        prompt=f"Apply the triaged route ({route})?",
        choices=(
            Choice("apply", "run the routed mode now"),
            Choice("abort", "stop; nothing is run"),
        ),
        context=context,
        asked_by=asked_by,
        fail_safe_key="abort",
    )


def replan_review_question(decision: Replan, *, asked_by: str) -> Question:
    """Supervise a manager-proposed re-plan: apply, revise, or reject.

    Default (unattended :class:`AutoChannel`) answer: apply — the manager
    re-plans autonomously when no human is watching. With no input available
    (EOF) the fail-safe is ``reject``: a detached run must not silently rewrite
    its own plan and push work down a new path no human blessed.
    """

    return Question(
        topic="replan-review",
        prompt=f"Apply this re-plan for {decision.failed_task_id}?",
        choices=(
            Choice("apply", "apply the re-plan"),
            Choice("revise", "propose a different re-plan", accepts_text=True),
            Choice("reject", "leave the task failed"),
        ),
        context=render_replan(decision),
        asked_by=asked_by,
        fail_safe_key="reject",
    )
