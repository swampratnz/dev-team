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
from .sdk import AgentResult, AgentRunner
from .trace import Tracer


@dataclass
class InstrumentedRunner:
    """An :class:`AgentRunner` that records cost and a trace span per call.

    Attributes:
        inner: The underlying runner that actually talks to a model.
        role: Role label used for budget attribution and trace naming.
        budget: Optional budget to record usage against (may raise if exceeded).
        tracer: Optional tracer to record a span per call.
    """

    inner: AgentRunner
    role: str
    budget: Optional[Budget] = None
    tracer: Optional[Tracer] = None

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
    ) -> AgentResult:
        span = None
        if self.tracer is not None:
            span = self.tracer.start("agent", self.role)
        result = await self.inner.run(
            prompt,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            model=model,
        )
        if self.tracer is not None:
            self.tracer.end(span, "error" if result.is_error else "ok")
        if self.budget is not None:
            self.budget.record(self.role, result)
        return result
