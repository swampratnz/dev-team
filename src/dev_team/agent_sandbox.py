"""Confines the agentic engineer's live SDK tool loop to its own workspace.

``ContainerCommandRunner`` (``dev_team.sandbox``) boxes the commands the
*orchestrator* runs (gates, setup, scans) but never touches the engineer's own
Bash/Read/Write/Edit/Glob/Grep tool loop, which the Claude Agent SDK runs directly
on the host, scoped only by ``cwd`` — a working-directory *default*, not an
access boundary. Nothing stops ``Bash`` from ``cd ../<sibling-job>`` or an
absolute path, or ``Read``/``Write``/``Edit`` from taking a ``file_path``
outside the workspace. See ``docs/SECURITY.md``.

:func:`workspace_containment_hook` builds a ``PreToolUse`` hook (the SDK's
``ClaudeAgentOptions.hooks`` mechanism) that denies any Bash/Read/Write/Edit/
Glob/Grep call whose target resolves outside a given workspace root, using
the same real-path/symlink-escape check as ``execution.py``'s
``LocalWorkspace._within_root``.

Read/Write/Edit/Glob/Grep are checked deterministically against the tool
call's structured path argument. Bash is checked with a best-effort string
scan for path-like tokens (absolute paths, ``..`` segments, and
``~``/``~user`` home-directory references) — this is explicitly
defense-in-depth, not a hard guarantee: a determined one-liner (env
expansion, base64, ``eval``) can still evade it. Anything the scan cannot
positively resolve as in-root is denied.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from claude_agent_sdk import HookContext, HookMatcher, PreToolUseHookInput

# Tools this hook inspects; the SDK matcher syntax for "any of these names".
GUARDED_TOOLS = "Bash|Read|Write|Edit|Glob|Grep"

# tool_input keys carrying a filesystem path, by tool name. Read/Write/Edit
# require it; a missing/malformed value is a malformed call (denied, not
# ignored). Glob's and Grep's ``path`` is optional in the tools' own schema —
# omitting it means "search from cwd", which is already the workspace root,
# so a missing value there is not an escape and is allowed.
_REQUIRED_PATH_KEYS: Dict[str, str] = {"Read": "file_path", "Write": "file_path", "Edit": "file_path"}
_OPTIONAL_PATH_KEYS: Dict[str, str] = {"Glob": "path", "Grep": "path"}

# An empty, no-op hook response: the SDK treats it as "no opinion", so the
# call proceeds exactly as it would with no hook installed at all.
_ALLOW: Dict[str, Any] = {}

# Shell metacharacters that separate distinct command tokens. Not a real shell
# parser (see module docstring) — good enough to pull out path-like tokens
# from ordinary commands without choking on pipes/redirection/subshells.
# Includes ``<``/``>`` so a redirection target glued to its operator (e.g.
# ``echo x>../out.txt``, no space) still splits into its own token instead of
# hiding inside a token that never matches the escape check, and a backtick
# so a `` `cmd ../x` `` substitution's closing token isn't left dangling.
_SHELL_SPLIT = re.compile(r"[\s;&|()<>`]+")


def _deny(reason: str) -> Dict[str, Any]:
    """A PreToolUse denial. ``reason`` must never include a filesystem path —
    only the workspace root's own relative position, never a sibling job's
    absolute path."""

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _resolves_within(root_real: str, candidate: str) -> bool:
    """Whether ``candidate`` (absolute, or relative to the root) stays inside
    ``root_real`` once symlinks are resolved to their real target — the same
    check ``execution.py``'s ``LocalWorkspace._within_root`` applies to
    orchestrator-side file I/O, extended here to the live SDK tool loop."""

    target = candidate if os.path.isabs(candidate) else os.path.join(root_real, candidate)
    try:
        real = os.path.realpath(target)
    except (OSError, ValueError):
        return False
    return os.path.commonpath([root_real, real]) == root_real


def _looks_like_escape(token: str) -> bool:
    """Whether ``token`` is an absolute path, contains a literal ``..`` path
    segment, or is a ``~``/``~user`` home-directory reference — the three
    shapes a Bash argument can use to leave the workspace root."""

    if token.startswith("/"):
        return True
    if token.startswith("~"):
        return True
    return ".." in token.split("/")


def _bash_escape_tokens(command: str) -> List[str]:
    """Path-like tokens in ``command`` worth resolving against the root."""

    return [t for t in _SHELL_SPLIT.split(command) if t and _looks_like_escape(t)]


def _expand_bash_token(token: str) -> Optional[str]:
    """Expand a Bash escape ``token`` the same way a shell would, before it is
    resolved against the workspace root. Only ``~``/``~user`` tokens need
    expansion; other tokens pass through unchanged. ``os.path.expanduser``
    silently returns its input unchanged when it cannot resolve a home
    directory (e.g. no ``HOME`` and no passwd entry) — treat that as a failed
    expansion and return ``None`` rather than letting the still-``~``-prefixed
    token fall through to :func:`_resolves_within`'s workspace-relative-join
    branch, which would wrongly treat it as a plain relative path and could
    allow it."""

    if not token.startswith("~"):
        return token
    expanded = os.path.expanduser(token)
    if expanded == token or expanded.startswith("~"):
        return None
    return expanded


def _check_bash(root_real: str, tool_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    command = tool_input.get("command")
    if not isinstance(command, str):
        return _deny("unable to inspect this Bash call's command")
    for token in _bash_escape_tokens(command):
        candidate = _expand_bash_token(token)
        if candidate is None or not _resolves_within(root_real, candidate):
            return _deny("Bash command references a path outside the job workspace")
    return None


def _check_path_tool(
    root_real: str, tool_name: str, tool_input: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if tool_name in _OPTIONAL_PATH_KEYS:
        value = tool_input.get(_OPTIONAL_PATH_KEYS[tool_name])
        if value is None:
            return None  # defaults to cwd, which is already the workspace root
    else:
        value = tool_input.get(_REQUIRED_PATH_KEYS[tool_name])
    if not isinstance(value, str) or not value:
        return _deny(f"unable to resolve this {tool_name} call's target path")
    if not _resolves_within(root_real, value):
        return _deny(f"{tool_name} target path escapes the job workspace")
    return None


def workspace_containment_hook(root: str) -> HookMatcher:
    """Build a ``PreToolUse`` hook confining Bash/Read/Write/Edit/Glob/Grep to ``root``.

    ``root`` is resolved to its real path once, at build time, so every call
    this hook denies or allows is checked against the same fixed boundary —
    callers that need the boundary to track a *live* cwd (e.g. a worktree
    created after this hook is built) should rebuild the hook for that cwd
    rather than reuse a stale one.
    """

    root_real = os.path.realpath(root)

    async def _hook(
        input_data: PreToolUseHookInput, tool_use_id: Optional[str], context: HookContext
    ) -> Dict[str, Any]:
        if not isinstance(input_data, dict):
            return _deny("malformed tool call")
        tool_name = input_data.get("tool_name")
        tool_input = input_data.get("tool_input")
        if not isinstance(tool_input, dict):
            return _deny("malformed tool call")
        if tool_name == "Bash":
            result = _check_bash(root_real, tool_input)
        elif tool_name in _REQUIRED_PATH_KEYS or tool_name in _OPTIONAL_PATH_KEYS:
            result = _check_path_tool(root_real, tool_name, tool_input)
        else:
            return _ALLOW
        return result if result is not None else _ALLOW

    return HookMatcher(matcher=GUARDED_TOOLS, hooks=[_hook])
