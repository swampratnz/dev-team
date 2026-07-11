"""Real side-effecting execution behind testable protocol boundaries.

This is the layer that turns dev-team from a pure simulation into a system that
actually manipulates a workspace and runs commands. Every side effect goes
through a small protocol (:class:`Workspace`, :class:`CommandRunner`) with both
a real implementation and an in-memory/fake one, so the rest of the system —
and its tests — never need to touch a real filesystem or spawn processes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, runtime_checkable

from .errors import DevTeamError


class WorkspaceError(DevTeamError):
    """Raised when a workspace operation is invalid (e.g. path escape)."""


# --------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------


@runtime_checkable
class Workspace(Protocol):
    """A sandboxed file store the team reads from and writes to."""

    def read_text(self, path: str) -> str:
        """Return the contents of ``path``."""
        ...

    def write_text(self, path: str, content: str) -> None:
        """Create or overwrite ``path`` with ``content``."""
        ...

    def exists(self, path: str) -> bool:
        """Whether ``path`` exists in the workspace."""
        ...

    def delete(self, path: str) -> None:
        """Remove ``path`` if present."""
        ...

    def list_files(self) -> List[str]:
        """Return all file paths, sorted."""
        ...


def _normalise(path: str) -> str:
    """Normalise a workspace-relative path, rejecting escapes and absolutes."""

    if path.startswith("/"):
        raise WorkspaceError(f"absolute paths are not allowed: {path!r}")
    parts: List[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspaceError(f"path escapes the workspace: {path!r}")
        parts.append(part)
    if not parts:
        raise WorkspaceError(f"empty path: {path!r}")
    return "/".join(parts)


class InMemoryWorkspace:
    """A dict-backed :class:`Workspace` for tests and dry runs."""

    def __init__(self, files: Optional[Dict[str, str]] = None) -> None:
        self._files: Dict[str, str] = {}
        for path, content in (files or {}).items():
            self.write_text(path, content)

    def read_text(self, path: str) -> str:
        key = _normalise(path)
        if key not in self._files:
            raise WorkspaceError(f"no such file: {path!r}")
        return self._files[key]

    def write_text(self, path: str, content: str) -> None:
        self._files[_normalise(path)] = content

    def exists(self, path: str) -> bool:
        return _normalise(path) in self._files

    def delete(self, path: str) -> None:
        self._files.pop(_normalise(path), None)

    def list_files(self) -> List[str]:
        return sorted(self._files)


# Directories that are tooling internals or dependency caches, never product
# code. Listing them bloats prompts (a repo's .git alone can be tens of
# thousands of paths) and leaks bookkeeping into reports.
DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".dev_team",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        ".venv",
        "venv",
    }
)


class LocalWorkspace:
    """A :class:`Workspace` rooted at a real directory on disk.

    ``list_files`` skips :data:`DEFAULT_EXCLUDED_DIRS` (override with
    ``excluded_dirs``); reads and writes are unaffected by the exclusions.
    """

    def __init__(self, root: str, *, excluded_dirs: Optional[frozenset] = None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.excluded_dirs = (
            excluded_dirs if excluded_dirs is not None else DEFAULT_EXCLUDED_DIRS
        )

    def _path(self, path: str) -> Path:
        return self.root / _normalise(path)

    def read_text(self, path: str) -> str:
        target = self._path(path)
        if not target.is_file():
            raise WorkspaceError(f"no such file: {path!r}")
        return target.read_text()

    def write_text(self, path: str, content: str) -> None:
        # Write-then-rename so a crash mid-write can never leave a truncated
        # file — checkpoints and memory are written on this path, and a
        # half-written checkpoint would brick the resume it exists for.
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_name(target.name + ".dev-team-tmp")
        staging.write_text(content)
        staging.replace(target)

    def exists(self, path: str) -> bool:
        return self._path(path).exists()

    def delete(self, path: str) -> None:
        target = self._path(path)
        if target.is_file():
            target.unlink()

    def list_files(self) -> List[str]:
        results = []
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            relative = p.relative_to(self.root)
            if any(part in self.excluded_dirs for part in relative.parts):
                continue
            results.append(str(relative).replace("\\", "/"))
        return sorted(results)


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------


@dataclass
class CommandResult:
    """The outcome of running a shell command."""

    command: Sequence[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command exited successfully."""

        return self.exit_code == 0

    @property
    def output(self) -> str:
        """Combined stdout and stderr, stripped."""

        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


@runtime_checkable
class CommandRunner(Protocol):
    """Runs shell commands and returns their result."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Execute ``command`` and return a :class:`CommandResult`."""
        ...


# Sentinels for the two non-standard exit codes we synthesise.
EXIT_NOT_FOUND = 127
EXIT_TIMEOUT = 124


@dataclass
class SubprocessCommandRunner:
    """A :class:`CommandRunner` backed by :mod:`subprocess`."""

    cwd: Optional[str] = None
    timeout: Optional[float] = None

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        args = list(command)
        try:
            proc = subprocess.run(
                args,
                cwd=cwd or self.cwd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout if timeout is not None else self.timeout,
            )
        except FileNotFoundError as exc:
            return CommandResult(args, EXIT_NOT_FOUND, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired carries *bytes* even under text=True; decoding
            # here keeps CommandResult.output usable instead of raising.
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            return CommandResult(args, EXIT_TIMEOUT, partial, "command timed out")
        return CommandResult(args, proc.returncode, proc.stdout, proc.stderr)


@dataclass
class DryRunCommandRunner:
    """A :class:`CommandRunner` that executes nothing, honestly.

    Every command "succeeds" with output that says it was not executed, so a
    dry run's gate reports are legible as dry-run results rather than
    masquerading as real verification. This is the default pairing for an
    :class:`InMemoryWorkspace`, where there is nothing on disk to run against.
    """

    calls: List[List[str]] = field(default_factory=list)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        args = list(command)
        self.calls.append(args)
        return CommandResult(args, 0, f"dry-run: {' '.join(args)} not executed", "")


@dataclass
class FakeCommandRunner:
    """A scripted :class:`CommandRunner` for tests.

    Results are matched by a substring of the joined command; the first
    matching rule wins. Unmatched commands return ``default`` and are recorded
    in :attr:`calls`.
    """

    rules: List[tuple] = field(default_factory=list)
    default_exit_code: int = 0
    calls: List[List[str]] = field(default_factory=list)

    def add_rule(self, match: str, result: CommandResult) -> "FakeCommandRunner":
        """Register ``result`` for commands containing ``match``."""

        self.rules.append((match, result))
        return self

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        args = list(command)
        self.calls.append(args)
        joined = " ".join(args)
        for match, result in self.rules:
            if match in joined:
                return result
        return CommandResult(args, self.default_exit_code, "", "")
