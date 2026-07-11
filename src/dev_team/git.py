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

    def is_repo(self) -> bool:
        """Whether the working directory is inside a git work tree."""

        result = self._git("rev-parse", "--is-inside-work-tree", check=False)
        return result.ok and result.stdout.strip() == "true"

    def ensure_repo(self) -> None:
        """Initialise a repository (with a usable identity) if none exists.

        A missing ``user.name``/``user.email`` makes ``git commit`` fail on a
        fresh machine, so a local identity is set when absent.
        """

        if not self.is_repo():
            self.init()
        for key, value in (("user.name", "dev-team"), ("user.email", "dev-team@localhost")):
            if not self._git("config", key, check=False).stdout.strip():
                self._git("config", key, value)

    def diff(self) -> str:
        """Return the combined staged and unstaged diff against HEAD."""

        return self._git("diff", "HEAD", check=False).stdout

    def discard_changes(self) -> None:
        """Hard-reset tracked files and remove untracked ones.

        Used to roll a workspace back to the last committed state when an
        agentic attempt fails its gates.
        """

        self._git("reset", "--hard", check=False)
        self._git("clean", "-fd")

    def current_branch(self) -> str:
        """Return the current branch name."""

        return self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def create_branch(self, name: str) -> None:
        """Create and switch to a new branch."""

        self._git("checkout", "-b", name)

    def checkout(self, name: str) -> None:
        """Switch to an existing branch."""

        self._git("checkout", name)

    def switch_to(self, name: str) -> None:
        """Create and switch to ``name``, or just switch if it already exists."""

        result = self._git("checkout", "-b", name, check=False)
        if not result.ok:
            self._git("checkout", name)

    def add_all(self) -> None:
        """Stage all changes."""

        self._git("add", "-A")

    def add_paths(self, paths: List[str]) -> None:
        """Stage only ``paths`` (a curated product change set, not add -A)."""

        if paths:
            self._git("add", "--", *paths)

    def commit(self, message: str, *, allow_empty: bool = False) -> None:
        """Commit staged changes with ``message``."""

        args = ["commit", "-m", message]
        if allow_empty:
            args.insert(1, "--allow-empty")
        self._git(*args)

    def has_commits(self) -> bool:
        """Whether the repository has at least one commit."""

        return self._git("rev-parse", "--verify", "HEAD", check=False).ok

    def rev_parse(self, ref: str = "HEAD") -> str:
        """Resolve ``ref`` to a commit sha (empty string on failure)."""

        return self._git("rev-parse", ref, check=False).stdout.strip()

    def worktree_add(self, path: str, branch: str) -> None:
        """Create a new worktree at ``path`` on a fresh ``branch`` from HEAD."""

        self._git("worktree", "add", "-b", branch, path)

    def worktree_remove(self, path: str) -> None:
        """Remove the worktree at ``path`` (best effort)."""

        self._git("worktree", "remove", "--force", path, check=False)

    def delete_branch(self, name: str) -> None:
        """Delete branch ``name`` (best effort)."""

        self._git("branch", "-D", name, check=False)

    def merge_squash(self, branch: str) -> None:
        """Stage ``branch``'s changes onto the current branch without committing."""

        self._git("merge", "--squash", branch)

    def reset_soft(self, ref: str) -> None:
        """Move the branch tip to ``ref`` keeping all changes staged."""

        self._git("reset", "--soft", ref)

    def has_changes(self) -> bool:
        """Whether the working tree has uncommitted changes."""

        return bool(self._git("status", "--porcelain").stdout.strip())

    def changed_files(self) -> List[str]:
        """Return changed paths, one entry per file (untracked included).

        ``-uall`` expands untracked directories into individual files so new
        files inside new directories are reported (and reviewable) one by one.
        Renames report the new path.
        """

        lines = self._git("status", "--porcelain", "-uall").stdout.splitlines()
        paths = []
        for line in lines:
            if not line.strip():
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ")[-1]
            paths.append(path)
        return paths
