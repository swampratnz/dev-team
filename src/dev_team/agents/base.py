"""Common behaviour shared by every role agent."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from ..errors import AgentResponseError, JSONExtractionError
from ..events import AgentEvent, Listener, emit
from ..json_utils import extract_json
from ..sdk import AgentResult, AgentRunner

_RETRY_INSTRUCTION = """\

Your previous response could not be used: {reason}.
Respond again with a single valid JSON object matching the requested shape,
and nothing else — no prose, no Markdown fences."""


class BaseAgent:
    """Base class wiring an :class:`AgentRunner` to a role.

    Subclasses set :attr:`role`, :attr:`stage`, and :attr:`system_prompt`, then
    call :meth:`ask_json` from their task-specific methods.
    """

    role: str = "agent"
    stage: str = "work"
    system_prompt: str = "You are a helpful software professional."

    def __init__(
        self,
        runner: AgentRunner,
        *,
        model: Optional[str] = None,
        listener: Optional[Listener] = None,
        json_retries: int = 1,
    ) -> None:
        if json_retries < 0:
            raise ValueError("json_retries must be non-negative")
        self.runner = runner
        self.model = model
        self.listener = listener
        self.json_retries = json_retries

    def _emit(self, message: str, detail: Optional[str] = None) -> None:
        emit(
            self.listener,
            AgentEvent(
                role=self.role,
                stage=self.stage,
                message=message,
                detail=detail,
            ),
        )

    async def ask(
        self,
        prompt: str,
        *,
        allowed_tools: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AgentResult:
        """Send ``prompt`` to the model and return the raw result."""

        self._emit("working")
        result = await self.runner.run(
            prompt,
            system_prompt=self.system_prompt,
            allowed_tools=allowed_tools,
            model=model or self.model,
            cwd=cwd,
        )
        self._emit("completed", detail=f"{result.num_turns} turn(s)")
        return result

    async def ask_json(
        self,
        prompt: str,
        *,
        allowed_tools: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Any:
        """Send ``prompt`` and parse the response as JSON.

        A response that errored or contains no valid JSON is retried up to
        :attr:`json_retries` times with a corrective instruction appended,
        so one malformed reply does not sink a whole run.

        Raises:
            AgentResponseError: If every attempt fails to yield valid JSON.
        """

        attempt_prompt = prompt
        last_text = ""
        for attempt in range(self.json_retries + 1):
            result = await self.ask(
                attempt_prompt, allowed_tools=allowed_tools, cwd=cwd, model=model
            )
            last_text = result.text
            reason = None
            if result.is_error:
                reason = "the agent call reported an error"
            else:
                try:
                    return extract_json(result.text)
                except JSONExtractionError:
                    reason = "it contained no valid JSON"
            if attempt < self.json_retries:
                self._emit("retrying", detail=reason)
                attempt_prompt = prompt + _RETRY_INSTRUCTION.format(reason=reason)
        raise AgentResponseError(self.role, last_text)
