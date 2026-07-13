"""A thin, testable git porcelain over a :class:`CommandRunner`.

Because every git call goes through the injected :class:`CommandRunner`, this
whole module is exercised in tests with a :class:`FakeCommandRunner` — no real
repository required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from .errors import DevTeamError
from .execution import CommandRunner


class GitError(DevTeamError):
    """Raised when a git command fails."""


@dataclass
class GitRepo:
    """Run git operations in a working directory via a command runner.

    Every command carries ``timeout`` so a wedged git (a hung commit hook, a
    GPG prompt on a headless box) fails the operation instead of freezing the
    whole run.
    """

    runner: CommandRunner
    cwd: Optional[str] = None
    timeout: Optional[float] = 120.0

    def _git(self, *args: str, check: bool = True):
        result = self.runner.run(["git", *args], cwd=self.cwd, timeout=self.timeout)
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

    def toplevel(self) -> str:
        """Return the repository's top-level directory (empty on failure)."""

        return self._git("rev-parse", "--show-toplevel", check=False).stdout.strip()

    def ensure_repo(self) -> None:
        """Initialise a repository (with a usable identity) if none exists.

        Being *inside* a work tree is not enough: a workspace nested in some
        larger repository would silently adopt the enclosing repo, and every
        branch switch, baseline commit, and rollback would act repo-wide. A
        repo is only reused when its top level is the working directory
        itself; otherwise a fresh one is initialised there.

        A missing ``user.name``/``user.email`` makes ``git commit`` fail on a
        fresh machine, so a local identity is set when absent.
        """

        top = self.toplevel()
        here = os.path.realpath(self.cwd) if self.cwd else os.path.realpath(os.getcwd())
        if not self.is_repo() or (top and os.path.realpath(top) != here):
            self.init()
        for key, value in (("user.name", "dev-team"), ("user.email", "dev-team@localhost")):
            if not self._git("config", key, check=False).stdout.strip():
                self._git("config", key, value)

    def diff(self) -> str:
        """Return the combined staged and unstaged diff against HEAD."""

        return self._git("diff", "HEAD", check=False).stdout

    def diff_names(self, ref: str) -> List[str]:
        """Return the tracked paths that differ between ``ref`` and the tree."""

        out = self._git("diff", "--name-only", ref, check=False).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

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
        """Resolve ``ref`` to a commit sha (empty string on failure).

        ``--verify --quiet`` matters: a bare ``rev-parse BADREF`` echoes the
        ref name to stdout while failing, which would hand callers a bogus
        "sha".
        """

        result = self._git("rev-parse", "--verify", "--quiet", ref, check=False)
        return result.stdout.strip() if result.ok else ""

    def worktree_add(self, path: str, branch: str) -> None:
        """Create a new worktree at ``path`` on ``branch``, reset to HEAD.

        ``-B`` (not ``-b``) so a stale branch left behind by a crashed run is
        reset and reused instead of failing the task on rerun.
        """

        self._git("worktree", "add", "-B", branch, path)

    def worktree_remove(self, path: str) -> None:
        """Remove the worktree at ``path`` (best effort)."""

        self._git("worktree", "remove", "--force", path, check=False)

    def worktree_prune(self) -> None:
        """Drop stale worktree registrations (best effort)."""

        self._git("worktree", "prune", check=False)

    def delete_branch(self, name: str) -> None:
        """Delete branch ``name`` (best effort)."""

        self._git("branch", "-D", name, check=False)

    def stash_push(self, paths: List[str]) -> bool:
        """Temporarily shelve changes to ``paths`` (untracked included).

        Returns whether anything was actually stashed; callers must only
        ``stash_pop`` when it was.
        """

        return self._git("stash", "push", "-u", "--", *paths, check=False).ok

    def stash_pop(self) -> None:
        """Restore the most recently stashed changes (best effort)."""

        self._git("stash", "pop", check=False)

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

        ``-z`` is essential, not cosmetic: without it, ``git status
        --porcelain`` C-quotes any path containing a space or non-ASCII byte
        (e.g. ``?? "my file.py"``), and staging that quoted string later
        (``git add -- '"my file.py"'``) exits 128 and aborts delivery. With
        ``-z`` the output is NUL-separated and paths are emitted verbatim, so
        they round-trip straight back into ``git add``.
        """

        # Records are NUL-separated (no trailing newline noise). Each record is
        # a two-char XY status, a space, then the path. A rename/copy (X in
        # {R, C}) is followed by its OLD path in the *next* NUL field, which we
        # consume and discard, keeping the NEW path from the status record.
        records = self._git("status", "--porcelain", "-uall", "-z").stdout.split("\0")
        paths: List[str] = []
        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if not record:
                continue
            paths.append(record[3:])
            if record[0] in ("R", "C"):
                i += 1  # skip the old-path field that follows a rename/copy
        return paths
