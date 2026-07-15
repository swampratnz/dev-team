"""Adapter over the Claude Agent SDK.

This module is the single integration boundary between dev-team and
``claude_agent_sdk``. Everything above it depends only on the small
:class:`AgentRunner` protocol, which keeps the rest of the system fully
testable without spawning the Claude CLI or making network calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol, Sequence, runtime_checkable

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    query,
)

# A single agent turn that takes longer than this has hung, not thought hard.
DEFAULT_TIMEOUT_SECONDS = 600.0


@dataclass
class AgentResult:
    """The distilled result of a single agent turn.

    Attributes:
        text: The concatenated assistant text output.
        cost_usd: Total cost reported by the SDK, if any.
        num_turns: Number of turns the SDK executed.
        model: The model that produced the result.
        is_error: Whether the SDK reported an error result.
    """

    text: str
    cost_usd: float = 0.0
    num_turns: int = 0
    model: Optional[str] = None
    is_error: bool = False


@runtime_checkable
class AgentRunner(Protocol):
    """Minimal async interface an agent uses to talk to a model.

    ``allowed_tools`` and ``cwd`` are what turn a call from a one-shot text
    completion into a real agent loop: when set, the model may read, edit, and
    run things inside ``cwd`` before answering.
    """

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> AgentResult:
        """Execute ``prompt`` and return an :class:`AgentResult`."""
        ...


def build_options(
    *,
    system_prompt: Optional[str],
    allowed_tools: Optional[Sequence[str]],
    model: Optional[str],
    permission_mode: str,
    cwd: Optional[str],
    max_turns: Optional[int],
) -> ClaudeAgentOptions:
    """Assemble :class:`ClaudeAgentOptions`, omitting unset values.

    ``allowed_tools`` is always set: ``None`` means an explicit *empty*
    allowlist, never "no restriction" — an agent gets exactly the tools its
    caller granted it.
    """

    kwargs: dict[str, Any] = {
        "permission_mode": permission_mode,
        "allowed_tools": list(allowed_tools) if allowed_tools is not None else [],
    }
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt
    if model is not None:
        kwargs["model"] = model
    if cwd is not None:
        kwargs["cwd"] = cwd
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    return ClaudeAgentOptions(**kwargs)


def extract_text(message: Any) -> List[str]:
    """Pull assistant text blocks out of an SDK message (duck-typed)."""

    content = getattr(message, "content", None)
    if not isinstance(content, (list, tuple)):
        return []
    texts: List[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            texts.append(text)
    return texts


@dataclass
class ClaudeAgentRunner:
    """Default :class:`AgentRunner` backed by ``claude_agent_sdk.query``.

    Attributes:
        default_model: Model used when a call does not override it.
        permission_mode: Permission mode passed to the SDK. The default,
            ``acceptEdits``, auto-accepts file edits but leaves other tools
            governed by ``allowed_tools`` — a per-call allowlist the SDK
            auto-permits. ``bypassPermissions`` is opt-in, never the default.
        cwd: Default working directory the agent operates in (a per-call
            ``cwd`` overrides it).
        max_turns: Optional cap on the number of SDK turns.
        timeout_seconds: Per-call wall-clock budget. A call that exceeds it
            (or raises an SDK/OS error) returns an error :class:`AgentResult`
            instead of raising, so callers' retry logic covers transient
            failures; :class:`asyncio.CancelledError` always propagates.
    """

    default_model: Optional[str] = None
    permission_mode: str = "acceptEdits"
    cwd: Optional[str] = None
    max_turns: Optional[int] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    _last_options: Optional[ClaudeAgentOptions] = field(
        default=None, repr=False, compare=False
    )

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> AgentResult:
        options = build_options(
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            model=model or self.default_model,
            permission_mode=self.permission_mode,
            cwd=cwd or self.cwd,
            max_turns=self.max_turns,
        )
        self._last_options = options
        try:
            return await asyncio.wait_for(
                self._consume(prompt, options, model), timeout=self.timeout_seconds
            )
        except (ClaudeSDKError, OSError, UnicodeDecodeError, TimeoutError) as exc:
            return AgentResult(
                text=f"{type(exc).__name__}: {exc}", is_error=True
            )

    async def _consume(
        self, prompt: str, options: ClaudeAgentOptions, model: Optional[str]
    ) -> AgentResult:
        return await _drain(query(prompt=prompt, options=options), model or self.default_model)


async def _drain(messages: Any, default_model: Optional[str]) -> AgentResult:
    """Fold an SDK message stream into an :class:`AgentResult`.

    Shared by the one-shot :func:`query` path (:class:`ClaudeAgentRunner`) and
    the persistent-session path (:class:`ClaudeAgentSession`) so both distil
    text, cost, turns, model, and the error flag identically.
    """

    texts: List[str] = []
    cost = 0.0
    num_turns = 0
    used_model = default_model
    is_error = False
    async for message in messages:
        texts.extend(extract_text(message))
        block_model = getattr(message, "model", None)
        if isinstance(block_model, str):
            used_model = block_model
        if hasattr(message, "total_cost_usd"):
            cost = getattr(message, "total_cost_usd", 0.0) or 0.0
            num_turns = getattr(message, "num_turns", 0) or 0
            is_error = bool(getattr(message, "is_error", False))
            result_text = getattr(message, "result", None)
            if isinstance(result_text, str) and result_text.strip():
                texts.append(result_text)
    return AgentResult(
        text="\n".join(texts),
        cost_usd=cost,
        num_turns=num_turns,
        model=used_model,
        is_error=is_error,
    )


def _default_client_factory(options: ClaudeAgentOptions) -> Any:
    """Construct the real SDK client (its subprocess starts on ``connect``)."""

    return ClaudeSDKClient(options=options)


@runtime_checkable
class ChatBackend(Protocol):
    """A multi-turn conversation that keeps context between messages."""

    async def send(self, text: str) -> str:
        """Send ``text`` and return the assistant's full reply."""
        ...

    async def close(self) -> None:
        """Release the underlying session."""
        ...


@dataclass
class ClaudeChatBackend:
    """A :class:`ChatBackend` on a persistent ``ClaudeSDKClient`` session.

    Unlike :class:`ClaudeAgentRunner` (one fresh SDK session per call), this
    holds a single conversation open across :meth:`send` calls, so the model
    retains everything said earlier — what a chat needs. The session has no
    tools: it is a conversation, not an agent loop.

    Attributes:
        system_prompt: The persona/system prompt for the conversation.
        model: Optional model override.
        client_factory: Injection point for tests; defaults to the real
            ``ClaudeSDKClient``.
    """

    system_prompt: str
    model: Optional[str] = None
    client_factory: Optional[Callable[[ClaudeAgentOptions], Any]] = None
    _client: Any = field(default=None, repr=False, compare=False)

    async def _ensure_client(self) -> Any:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "system_prompt": self.system_prompt,
                "allowed_tools": [],
            }
            if self.model is not None:
                kwargs["model"] = self.model
            options = ClaudeAgentOptions(**kwargs)
            factory = self.client_factory or _default_client_factory
            self._client = factory(options)
            await self._client.connect()
        return self._client

    async def send(self, text: str) -> str:
        client = await self._ensure_client()
        await client.query(text)
        texts: List[str] = []
        async for message in client.receive_response():
            texts.extend(extract_text(message))
        return "\n".join(texts)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


@runtime_checkable
class AgentSession(Protocol):
    """A persistent, tool-enabled conversation reused across turns.

    Unlike :meth:`AgentRunner.run` (a fresh SDK session per call), an
    ``AgentSession`` holds one client open, so a later turn retains everything
    from earlier ones — the code the model read, the changes it made. Each turn
    returns a full :class:`AgentResult`, so it meters exactly as ``run`` does.
    """

    async def send(self, prompt: str) -> AgentResult:
        """Send one turn in the open conversation and return its result."""
        ...

    async def aclose(self) -> None:
        """Release the underlying session."""
        ...


@dataclass
class ClaudeAgentSession:
    """A tool-enabled :class:`AgentSession` on a persistent ``ClaudeSDKClient``.

    Like :class:`ClaudeChatBackend`, but the session carries
    ``allowed_tools``/``cwd``/``permission_mode`` so the model can read, edit,
    and run inside ``cwd`` across turns — the transport an engineer needs to
    *continue* a prior attempt rather than restart it cold. The system prompt,
    tools, and cwd are fixed for the session's life; only the per-turn prompt
    varies.

    A failed turn (SDK/OS error or timeout) returns an error
    :class:`AgentResult` rather than raising, matching
    :class:`ClaudeAgentRunner`; ``timeout_seconds`` bounds the *whole* turn
    (connect, query, and response), not just the response stream, so a wedged
    client can't block a shared-pool worker forever.
    :class:`asyncio.CancelledError` always propagates.

    An error result may leave the underlying client wedged, and ``send`` reuses
    it (``_ensure_client`` only reconnects when the client is ``None``). Callers
    that want to retry should therefore *discard the session and construct a new
    one* — "fall back to a fresh session" means a fresh :class:`ClaudeAgentSession`,
    not another ``send`` on this instance.
    """

    system_prompt: Optional[str] = None
    allowed_tools: Optional[Sequence[str]] = None
    model: Optional[str] = None
    cwd: Optional[str] = None
    permission_mode: str = "acceptEdits"
    max_turns: Optional[int] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    client_factory: Optional[Callable[[ClaudeAgentOptions], Any]] = None
    _client: Any = field(default=None, repr=False, compare=False)

    async def _ensure_client(self) -> Any:
        if self._client is None:
            options = build_options(
                system_prompt=self.system_prompt,
                allowed_tools=self.allowed_tools,
                model=self.model,
                permission_mode=self.permission_mode,
                cwd=self.cwd,
                max_turns=self.max_turns,
            )
            factory = self.client_factory or _default_client_factory
            self._client = factory(options)
            await self._client.connect()
        return self._client

    async def send(self, prompt: str) -> AgentResult:
        try:
            return await asyncio.wait_for(self._turn(prompt), timeout=self.timeout_seconds)
        except (ClaudeSDKError, OSError, UnicodeDecodeError, TimeoutError) as exc:
            return AgentResult(text=f"{type(exc).__name__}: {exc}", is_error=True)

    async def _turn(self, prompt: str) -> AgentResult:
        # The whole turn — connect, query, and drain — runs under send()'s
        # timeout, matching ClaudeAgentRunner (which wraps all of _consume): a
        # hang in connect() or query(), not just in the response stream, must
        # still surface as an error result rather than blocking a shared-pool
        # worker forever.
        client = await self._ensure_client()
        await client.query(prompt)
        return await _drain(client.receive_response(), self.model)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


@dataclass
class FakeAgentSession:
    """A scripted :class:`AgentSession` for tests.

    Records every prompt; returns the queued results in order (repeating the
    last), or an empty success when none are queued.
    """

    results: List[AgentResult] = field(default_factory=list)
    prompts: List[str] = field(default_factory=list)
    closed: bool = False

    async def send(self, prompt: str) -> AgentResult:
        self.prompts.append(prompt)
        if not self.results:
            return AgentResult(text="", num_turns=1)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    async def aclose(self) -> None:
        self.closed = True
