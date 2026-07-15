"""Instrumentation wrapper that meters and traces every agent call.

Wrapping an :class:`~dev_team.sdk.AgentRunner` in an
:class:`InstrumentedRunner` gives budget accounting and audit tracing to any
agent without the agent knowing about it — the same seam the SDK already sits
behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .budget import Budget
from .sdk import AgentResult, AgentRunner, AgentSession
from .trace import Tracer
from .transcripts import TranscriptRecorder


@dataclass
class InstrumentedRunner:
    """An :class:`AgentRunner` that records cost and a trace span per call.

    Attributes:
        inner: The underlying runner that actually talks to a model.
        role: Role label used for budget attribution and trace naming.
        budget: Optional budget to record usage against (may raise if exceeded).
        tracer: Optional tracer to record a span per call.
        transcript_recorder: Optional recorder that captures the raw
            system-prompt/prompt/response of each call to disk (off by
            default). Recording is best-effort: a write failure is swallowed
            so it can never break a run.
    """

    inner: AgentRunner
    role: str
    budget: Optional[Budget] = None
    tracer: Optional[Tracer] = None
    transcript_recorder: Optional[TranscriptRecorder] = None

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> AgentResult:
        if self.budget is not None:
            # Pre-flight: refuse to spend anything once the ceiling is hit.
            self.budget.check()
        span = None
        if self.tracer is not None:
            span = self.tracer.start("agent", self.role)
        try:
            result = await self.inner.run(
                prompt,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                model=model,
                cwd=cwd,
            )
        except BaseException:
            # A raising call must not leave its trace span open forever. Its
            # cost is unknowable here (the SDK surfaces usage only on
            # results), so nothing is recorded against the budget.
            if self.tracer is not None:
                self.tracer.end(span, "exception")
            raise
        if self.tracer is not None:
            self.tracer.end(span, "error" if result.is_error else "ok")
        # Capture the raw I/O on both the success and the error-result paths
        # (the raising path returned above, so it has no result to record).
        # This runs BEFORE the budget is enforced: budget.record raises once
        # the ceiling is crossed, and the call whose cost tips it over has
        # still been paid for — its transcript must be audited, not lost to
        # the exception.
        if self.transcript_recorder is not None:
            self._record(system_prompt, prompt, result)
        if self.budget is not None:
            self.budget.record(self.role, result)
        return result

    def _record(
        self, system_prompt: Optional[str], prompt: str, result: AgentResult
    ) -> None:
        """Best-effort transcript write; a failure must never break a run."""

        try:
            self.transcript_recorder.record(
                role=self.role,
                system_prompt=system_prompt,
                prompt=prompt,
                result=result,
            )
        except Exception:  # noqa: BLE001 - recording is forgiving, like the eventlog
            pass


@dataclass
class InstrumentedSession:
    """An :class:`~dev_team.sdk.AgentSession` that meters and traces each turn.

    The session equivalent of :class:`InstrumentedRunner`: every
    :meth:`send` records cost against the budget, opens/closes a trace span, and
    captures the transcript — identical accounting, so reusing a session across
    attempts never loses budget or audit coverage. ``system_prompt`` is fixed
    for the session's life, so it is held here for transcript attribution rather
    than passed per turn.
    """

    inner: AgentSession
    role: str
    budget: Optional[Budget] = None
    tracer: Optional[Tracer] = None
    transcript_recorder: Optional[TranscriptRecorder] = None
    system_prompt: Optional[str] = None

    async def send(self, prompt: str) -> AgentResult:
        if self.budget is not None:
            self.budget.check()  # pre-flight: refuse to spend past the ceiling
        span = None
        if self.tracer is not None:
            span = self.tracer.start("agent", self.role)
        try:
            result = await self.inner.send(prompt)
        except BaseException:
            if self.tracer is not None:
                self.tracer.end(span, "exception")
            raise
        if self.tracer is not None:
            self.tracer.end(span, "error" if result.is_error else "ok")
        # Record before enforcing the budget: the call that tips the ceiling has
        # still been paid for, so its transcript must be audited, not lost.
        if self.transcript_recorder is not None:
            self._record(prompt, result)
        if self.budget is not None:
            self.budget.record(self.role, result)
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()

    def _record(self, prompt: str, result: AgentResult) -> None:
        """Best-effort transcript write; a failure must never break a run."""

        try:
            self.transcript_recorder.record(
                role=self.role,
                system_prompt=self.system_prompt,
                prompt=prompt,
                result=result,
            )
        except Exception:  # noqa: BLE001 - recording is forgiving, like the eventlog
            pass
