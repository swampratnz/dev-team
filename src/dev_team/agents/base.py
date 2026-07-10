"""Common behaviour shared by every role agent."""

from __future__ import annotations

from typing import Any, Optional

from ..errors import AgentResponseError, JSONExtractionError
from ..events import AgentEvent, Listener, emit
from ..json_utils import extract_json
from ..sdk import AgentResult, AgentRunner


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
    ) -> None:
        self.runner = runner
        self.model = model
        self.listener = listener

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

    async def ask(self, prompt: str) -> AgentResult:
        """Send ``prompt`` to the model and return the raw result."""

        self._emit("working")
        result = await self.runner.run(
            prompt,
            system_prompt=self.system_prompt,
            model=self.model,
        )
        self._emit("completed", detail=f"{result.num_turns} turn(s)")
        return result

    async def ask_json(self, prompt: str) -> Any:
        """Send ``prompt`` and parse the response as JSON.

        Raises:
            AgentResponseError: If the response contains no valid JSON.
        """

        result = await self.ask(prompt)
        try:
            return extract_json(result.text)
        except JSONExtractionError as exc:
            raise AgentResponseError(self.role, result.text) from exc
