"""Test doubles for driving the system without the real Agent SDK.

These helpers are part of the public package so that users writing their own
tests (or dry-running the workflow) can supply canned agent responses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Union

from .sdk import AgentResult

Response = Union[str, "AgentResult"]


def _to_result(response: Response) -> AgentResult:
    if isinstance(response, AgentResult):
        return response
    return AgentResult(text=response, num_turns=1)


class ScriptedRunner:
    """An :class:`~dev_team.sdk.AgentRunner` that replays canned responses.

    Responses may be provided as a flat queue (consumed in call order) and/or
    keyed by a substring that must appear in the system prompt. Keyed matches
    take priority, which lets a test target a specific role. Each string is
    returned verbatim as the agent's text output.
    """

    def __init__(
        self,
        responses: Optional[Sequence[Response]] = None,
        *,
        by_system_prompt: Optional[Dict[str, Response]] = None,
    ) -> None:
        self._queue: List[Response] = list(responses or [])
        self._keyed: Dict[str, Response] = dict(by_system_prompt or {})
        self.calls: List[Dict[str, Any]] = []

    def add(self, response: Response) -> "ScriptedRunner":
        """Append a response to the queue and return self for chaining."""

        self._queue.append(response)
        return self

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "allowed_tools": allowed_tools,
                "model": model,
            }
        )
        if system_prompt is not None:
            for key, response in self._keyed.items():
                if key in system_prompt:
                    return _to_result(response)
        if self._queue:
            return _to_result(self._queue.pop(0))
        raise AssertionError("ScriptedRunner ran out of responses")


def json_response(payload: Any) -> str:
    """Serialise ``payload`` to a JSON string wrapped in a Markdown fence.

    Wrapping in prose/fences mirrors real model output and exercises the JSON
    extraction path.
    """

    return f"Here you go:\n```json\n{json.dumps(payload)}\n```\nDone."
