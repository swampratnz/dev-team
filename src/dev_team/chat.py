"""A conversational front door: shape a feature request, then run it.

``dev-team --chat`` opens a multi-turn conversation with the product manager
persona. You describe what you want, the PM asks clarifying questions and
pushes back on vague scope; when the brief is solid you say ``/run`` (or
``/deliver``) and the same conversation's understanding — distilled into a
:class:`~dev_team.models.FeatureRequest` — is handed to the team.

The conversation itself runs on a :class:`~dev_team.sdk.ChatBackend`: a
persistent session that keeps context between messages (the real
implementation is :class:`~dev_team.sdk.ClaudeChatBackend`). Running the
feature is delegated to a callback, so this module knows nothing about
engines, workspaces, or budgets.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, TextIO

from .errors import JSONExtractionError
from .json_utils import extract_json
from .models import FeatureRequest
from .persona import Persona
from .sdk import ChatBackend

#: System prompt for the chat conversation. Unlike the run-time PM prompt it
#: is conversational — free text is expected — but the brief handoff at the
#: end reuses the same JSON-object discipline the rest of the system relies on.
CHAT_SYSTEM_PROMPT = """\
{preamble}You are the product manager of an AI software development team,
talking with the person who wants a feature built. Help them shape the idea
into a concrete, buildable feature request: ask clarifying questions (one or
two at a time, not a wall), surface hidden assumptions, challenge scope that
will not fit a small iteration, and steer acceptance criteria towards things
an automated test could verify. Be concise and concrete.

When — and only when — you are asked for the final brief, respond with a
single JSON object and nothing else, shaped exactly like:
{{"title": "...", "description": "...", "constraints": ["..."]}}"""

BRIEF_PROMPT = (
    "Produce the final brief for the feature as discussed. Respond with the "
    "single JSON object only."
)

_HELP = """\
Talk to shape the feature. Commands:
  /run      hand the brief to the team (simulation — no side effects)
  /deliver  hand the brief to the team for real delivery
  /help     show this help
  /quit     leave the chat"""

#: run_feature(request, deliver) -> process exit code for that run.
RunFeature = Callable[[FeatureRequest, bool], Awaitable[int]]


def chat_system_prompt(persona: Optional[Persona]) -> str:
    """The chat system prompt, introduced by ``persona`` when cast."""

    preamble = f"{persona.preamble()} " if persona is not None else ""
    return CHAT_SYSTEM_PROMPT.format(preamble=preamble)


def _request_from_brief(data: dict) -> FeatureRequest:
    """Validate a brief JSON object into a :class:`FeatureRequest`."""

    title = data.get("title")
    description = data.get("description")
    if not isinstance(title, str) or not title.strip():
        raise JSONExtractionError(str(data))
    if not isinstance(description, str) or not description.strip():
        raise JSONExtractionError(str(data))
    raw_constraints = data.get("constraints", [])
    constraints = [
        c for c in raw_constraints if isinstance(c, str) and c.strip()
    ] if isinstance(raw_constraints, list) else []
    return FeatureRequest(
        title=title.strip(), description=description.strip(), constraints=constraints
    )


@dataclass
class ChatSession:
    """The ``--chat`` REPL.

    Attributes:
        backend: The conversation (kept for the whole session).
        run_feature: Callback that actually runs the team on the agreed
            brief; receives ``deliver=True`` for ``/deliver``.
        pm_name: Display name for the conversational partner.
        input_fn: Injection point for tests (defaults to ``input``).
        output: Stream for the conversation (defaults to stdout).
    """

    backend: ChatBackend
    run_feature: RunFeature
    pm_name: str = "product-manager"
    input_fn: Callable[[str], str] = input
    output: Optional[TextIO] = None
    _last_exit: int = field(default=0, repr=False)

    def _write(self, text: str) -> None:
        stream = self.output if self.output is not None else sys.stdout
        stream.write(text + "\n")
        stream.flush()

    async def _read(self, prompt: str) -> Optional[str]:
        try:
            return await asyncio.to_thread(self.input_fn, prompt)
        except (EOFError, OSError):
            # EOF and a closed stream (e.g. BrokenPipeError from a piped
            # stdout) both mean the conversation partner is gone.
            return None

    async def _say(self, text: str) -> None:
        reply = await self.backend.send(text)
        self._write(f"{self.pm_name} > {reply}")

    async def _brief(self) -> Optional[FeatureRequest]:
        """Distil the conversation into a request, or ``None`` on failure."""

        reply = await self.backend.send(BRIEF_PROMPT)
        try:
            data = extract_json(reply)
            if not isinstance(data, dict):
                raise JSONExtractionError(reply)
            return _request_from_brief(data)
        except JSONExtractionError:
            self._write(
                "could not distil a brief from the conversation — keep "
                "refining it, then try again"
            )
            return None

    async def _hand_off(self, deliver: bool) -> None:
        request = await self._brief()
        if request is None:
            return
        mode = "delivery" if deliver else "simulation"
        # Running the team costs money (delivery also writes files and commits),
        # so show the distilled brief and require an explicit go-ahead. Anything
        # that is not a clear yes — including EOF — cancels (fail safe).
        self._write(f"distilled brief ({mode}):")
        self._write(f"  title:       {request.title}")
        self._write(f"  description: {request.description}")
        if request.constraints:
            self._write("  constraints:")
            for constraint in request.constraints:
                self._write(f"    - {constraint}")
        answer = await self._read(f"hand this to the team for {mode}? [y/N] ")
        if answer is None or answer.strip().lower() not in ("y", "yes"):
            self._write("cancelled; keep refining the brief (/quit to leave)")
            return
        self._write(
            f"handing off to the team ({mode}): {request.title} — "
            f"{request.description}"
        )
        self._last_exit = await self.run_feature(request, deliver)
        verdict = "succeeded" if self._last_exit == 0 else "finished with issues"
        self._write(f"run {verdict}; you are back in the chat (/quit to leave)")

    async def run(self) -> int:
        """Run the REPL until ``/quit`` or EOF; returns the last run's exit code."""

        self._write(
            f"chatting with {self.pm_name} — describe the feature you want "
            "(/help for commands)"
        )
        try:
            while True:
                line = await self._read("you > ")
                if line is None or line.strip() == "/quit":
                    return self._last_exit
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped == "/help":
                    self._write(_HELP)
                elif stripped == "/run":
                    await self._hand_off(deliver=False)
                elif stripped == "/deliver":
                    await self._hand_off(deliver=True)
                elif stripped.startswith("/"):
                    self._write(f"unknown command {stripped!r} (/help for commands)")
                else:
                    await self._say(stripped)
        finally:
            await self.backend.close()
