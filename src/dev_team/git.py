"""A thin, testable git porcelain over a :class:`CommandRunner`.

Because every git call goes through the injected :class:`CommandRunner`, this
whole module is exercised in tests with a :class:`FakeCommandRunner` — no real
repository required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .errors import DevTeamError
from .execution import CommandRunner


class GitError(DevTeamError):
    """Raised when a git command fails."""


@dataclass
class GitRepo:
    """Run git operations in a working directory via a command runner."""

    runner: CommandRunner
    cwd: Optional[str] = None

    def _git(self, *args: str, check: bool = True):
        result = self.runner.run(["git", *args], cwd=self.cwd)
        if check and not result.ok:
            raise GitError(
                f"git {' '.join(args)} failed ({result.exit_code}): {result.output}"
            )
        return result

    def init(self) -> None:
        """Initialise a repository."""

        self._git("init")

    def current_branch(self) -> str:
        """Return the current branch name."""

        return self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def create_branch(self, name: str) -> None:
        """Create and switch to a new branch."""

        self._git("checkout", "-b", name)

    def checkout(self, name: str) -> None:
        """Switch to an existing branch."""

        self._git("checkout", name)

    def add_all(self) -> None:
        """Stage all changes."""

        self._git("add", "-A")

    def commit(self, message: str) -> None:
        """Commit staged changes with ``message``."""

        self._git("commit", "-m", message)

    def has_changes(self) -> bool:
        """Whether the working tree has uncommitted changes."""

        return bool(self._git("status", "--porcelain").stdout.strip())

    def changed_files(self) -> List[str]:
        """Return the paths reported by ``git status --porcelain``."""

        lines = self._git("status", "--porcelain").stdout.splitlines()
        return [line[3:] for line in lines if line.strip()]
