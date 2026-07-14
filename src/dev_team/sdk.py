"""Adapter over the Claude Agent SDK.

This module is the single integration boundary between dev-team and
``claude_agent_sdk``. Everything above it depends only on the small
:class:`AgentRunner` protocol, which keeps the rest of the system fully
testable without spawning the Claude CLI or making network calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable

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
        texts: List[str] = []
        cost = 0.0
        num_turns = 0
        used_model = model or self.default_model
        is_error = False

        async for message in query(prompt=prompt, options=options):
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


@dataclass
class _Session:
    """One task's conversation: its client (if connected) and what it heard.

    ``client`` is ``None`` for a placeholder session — one that has never
    connected yet, or one recovering from a fault (see
    :meth:`SessionAgentRunner._reconnect`) — as opposed to a live, queryable
    connection.

    ``history`` holds every prompt sent on this session (in order). It exists
    only to rebuild full context if the session ever has to be discarded
    mid-task (see :meth:`SessionAgentRunner._reconnect`) — the whole point of
    continuity is that later attempts send a compact, feedback-only prompt,
    so once a client drops there is nowhere else full context could come from.
    """

    client: Optional[Any]
    model: Optional[str]
    system_prompt: Optional[str]
    allowed_tools: Optional[Sequence[str]]
    cwd: Optional[str]
    history: List[str] = field(default_factory=list)


@dataclass
class SessionAgentRunner:
    """An opt-in :class:`AgentRunner` that holds one persistent session per task.

    Unlike :class:`ClaudeAgentRunner` (a fresh SDK session on every call),
    this keeps a single ``ClaudeSDKClient`` conversation open across an
    engineering task's retry attempts, keyed by an explicit ``task_key``
    passed to :meth:`run` — mirroring :class:`ClaudeChatBackend`'s proven
    connect/query/receive/disconnect shape, just keyed per task instead of
    held as one global conversation. Not wired into any role by default;
    :class:`~dev_team.engine.DeliveryEngine` substitutes it for the
    engineer's runner only when ``EngineConfig.session_continuity`` is set.

    ``task_key`` is an extension beyond the :class:`AgentRunner` protocol's
    formal signature: :class:`~dev_team.instrument.InstrumentedRunner` only
    forwards it when a caller actually supplies one (see its docstring), so
    every other runner — including :class:`ClaudeAgentRunner` and every test
    double in the suite — never has to know this parameter exists.

    Fail-secure behaviour (see :meth:`_reconnect`): a continuation call whose
    transport drops, whose connect or query wall-clock budget is exceeded, or
    whose requested model differs from the one the session was connected
    with (``EngineConfig.escalation_model`` on the final attempt — a
    ``ClaudeSDKClient``'s model is fixed at ``connect()``), is never silently
    absorbed. The stale client is discarded and a fresh one is connected,
    replaying the task's full prompt history so the attempt still carries
    full context even though only this turn's compact prompt was handed to
    :meth:`run`. That accumulated history survives even a *failed* recovery —
    a reconnect whose own ``connect()`` or replay ``query()`` fails leaves a
    placeholder session (no live client) holding everything recorded so far,
    so the next call retries the same recovery with full context intact
    rather than silently starting over from just that turn's compact prompt.

    Attributes:
        permission_mode: Permission mode for every connected client (see
            :class:`ClaudeAgentRunner` — engineering needs edit permissions).
        max_turns: Optional cap on SDK turns per connected client.
        timeout_seconds: Wall-clock budget for each individual ``connect()``
            or query/receive turn (see :meth:`_connect`/:meth:`_query`) —
            bounded per call, not once for the whole of :meth:`run`, so a
            timeout on a reused session is an ordinary fault :meth:`_run`
            catches and routes through :meth:`_reconnect`/:meth:`_drop`
            exactly like a dropped connection, rather than a cancellation
            that would leave a half-consumed client sitting in
            ``_sessions``.
        client_factory: Injection point for tests; defaults to the real
            ``ClaudeSDKClient``.
    """

    permission_mode: str = "acceptEdits"
    max_turns: Optional[int] = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    client_factory: Optional[Callable[[ClaudeAgentOptions], Any]] = None
    _sessions: Dict[str, "_Session"] = field(
        default_factory=dict, repr=False, compare=False
    )

    async def run(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        task_key: Optional[str] = None,
    ) -> AgentResult:
        try:
            return await self._run(
                prompt,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                model=model,
                cwd=cwd,
                task_key=task_key,
            )
        except (ClaudeSDKError, OSError, UnicodeDecodeError, TimeoutError) as exc:
            return AgentResult(text=f"{type(exc).__name__}: {exc}", is_error=True)

    async def _run(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str],
        allowed_tools: Optional[Sequence[str]],
        model: Optional[str],
        cwd: Optional[str],
        task_key: Optional[str],
    ) -> AgentResult:
        if task_key is None:
            # No key: nothing to hold onto between calls, so behave like a
            # one-shot runner. The engine always supplies a key when
            # continuity is active; this is a defensive default only.
            client = await self._connect(
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                model=model,
                cwd=cwd,
            )
            try:
                return await self._query(client, prompt)
            finally:
                await client.disconnect()

        session = self._sessions.get(task_key)

        if (
            session is not None
            and session.client is not None
            and session.model == model
        ):
            # NOTE: allowed_tools/system_prompt/cwd are just as fixed at
            # connect() time as model but are deliberately NOT compared here
            # — both call sites in engine.py pass the same values on every
            # attempt for a given task_key, so a mismatch can't currently
            # happen. If that ever changes (e.g. a narrower allowlist
            # introduced between attempts), this check needs to grow to cover
            # them too, or a narrowed-tools attempt would silently keep
            # running with the wider, connect-time allowlist.
            try:
                result = await self._query(session.client, prompt)
            except (ClaudeSDKError, OSError, UnicodeDecodeError, TimeoutError):
                return await self._reconnect(
                    task_key,
                    session,
                    prompt,
                    system_prompt=system_prompt,
                    allowed_tools=allowed_tools,
                    model=model,
                    cwd=cwd,
                )
            session.history.append(prompt)
            return result

        # No live client for this task_key — either it has never been opened,
        # or a previous _reconnect's own connect()/query() failed and left
        # only its accumulated history behind (see _reconnect) — or the
        # requested model no longer matches the connected one (fixed at
        # connect() time; silently continuing on the stale model would defeat
        # escalation_model). Either way, (re)open fresh through the same path
        # so a session that has never even connected once and a session
        # recovering from a fault are handled identically.
        return await self._reconnect(
            task_key,
            session,
            prompt,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            model=model,
            cwd=cwd,
        )

    async def _reconnect(
        self,
        task_key: str,
        stale: Optional["_Session"],
        prompt: str,
        *,
        system_prompt: Optional[str],
        allowed_tools: Optional[Sequence[str]],
        model: Optional[str],
        cwd: Optional[str],
    ) -> AgentResult:
        """(Re)open ``task_key`` and replay ``stale``'s history fresh, on ``model``.

        The caller may have sent only this turn's compact follow-up (session
        continuity's whole point), so ``stale``'s recorded history — if
        any — is the only place full context can still come from. ``stale``
        is ``None`` on a task's very first call.

        A placeholder session (``client=None``, carrying ``stale``'s history
        but not yet this turn's prompt) is registered *before* the stale
        client is disconnected or a new one connected, so that if any of the
        steps below fails — disconnecting the old client, connecting a new
        one, or the replay query itself — the accumulated history is not
        lost with it: the next call for this ``task_key`` finds this
        placeholder (no live client, same history) and retries the same
        recovery instead of silently falling back to a bare, context-free
        session.
        """

        history = list(stale.history) if stale is not None else []
        replay = "\n\n".join(history + [prompt]) if history else prompt
        session = _Session(
            client=None,
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            cwd=cwd,
            history=history,
        )
        self._sessions[task_key] = session
        if stale is not None and stale.client is not None:
            await stale.client.disconnect()
        client = await self._connect(
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            model=model,
            cwd=cwd,
        )
        # Registered before the query runs, same reasoning as the docstring
        # above: a query that fails with something outside the fail-secure
        # catch below (e.g. cancellation) must still leave a connected client
        # close()/_drop() can find, exactly like the very first open used to.
        session.client = client
        try:
            result = await self._query(client, replay)
        except (ClaudeSDKError, OSError, UnicodeDecodeError, TimeoutError):
            # A client that fails its very first query is just as unsafe to
            # reuse as one that fails mid-continuation (see the timeout fix
            # above) — disconnect it and clear it from the session (but keep
            # the placeholder and its history) so the next call retries the
            # recovery with a fresh connect instead of reusing this
            # half-consumed client.
            await client.disconnect()
            session.client = None
            raise
        session.history.append(replay)
        return result

    async def _connect(
        self,
        *,
        system_prompt: Optional[str],
        allowed_tools: Optional[Sequence[str]],
        model: Optional[str],
        cwd: Optional[str],
    ) -> Any:
        options = build_options(
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            model=model,
            permission_mode=self.permission_mode,
            cwd=cwd,
            max_turns=self.max_turns,
        )
        factory = self.client_factory or _default_client_factory
        client = factory(options)
        # Bounded per-call, not by an outer wrap of the whole _run(): a
        # timeout raised *here* is an ordinary TimeoutError our own
        # try/except clauses catch, so a hung connect() routes through the
        # same _reconnect/_drop fail-secure path as every other fault
        # instead of surfacing as an uncatchable CancelledError further up.
        await asyncio.wait_for(client.connect(), timeout=self.timeout_seconds)
        return client

    async def _query(self, client: Any, text: str) -> AgentResult:
        # See _connect: timing out *this* coroutine (not the caller's) turns
        # a hang into a plain TimeoutError the caller's except clauses can
        # catch, so it self-heals via _reconnect/_drop instead of leaving a
        # half-consumed client sitting in self._sessions for the next call.
        return await asyncio.wait_for(
            self._receive(client, text), timeout=self.timeout_seconds
        )

    async def _receive(self, client: Any, text: str) -> AgentResult:
        await client.query(text)
        texts: List[str] = []
        cost = 0.0
        num_turns = 0
        used_model: Optional[str] = None
        is_error = False
        async for message in client.receive_response():
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

    async def _drop(self, task_key: str) -> None:
        session = self._sessions.pop(task_key, None)
        if session is not None and session.client is not None:
            await session.client.disconnect()

    async def close(self, task_key: str) -> None:
        """Disconnect and drop the session for ``task_key``, if any.

        Safe to call for a key with no live session (a task whose loop exited
        before ever calling :meth:`run`, or a repeat close) — a no-op rather
        than a stale-reference crash.
        """

        await self._drop(task_key)
