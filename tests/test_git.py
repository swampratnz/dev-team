"""Tests for the git porcelain (over a fake command runner)."""

from __future__ import annotations

import pytest

from dev_team.execution import CommandResult, FakeCommandRunner
from dev_team.git import GitError, GitRepo


def _runner(**rules):
    runner = FakeCommandRunner()
    for match, result in rules.items():
        runner.add_rule(match, result)
    return runner


def test_init_add_commit():
    runner = FakeCommandRunner()  # all commands succeed (exit 0)
    repo = GitRepo(runner, cwd="/work")
    repo.init()
    repo.add_all()
    repo.commit("msg")
    repo.checkout("main")
    repo.create_branch("feature")
    joined = [" ".join(c) for c in runner.calls]
    assert "git init" in joined
    assert "git add -A" in joined
    assert "git commit -m msg" in joined
    assert "git checkout main" in joined
    assert "git checkout -b feature" in joined


def test_current_branch():
    runner = FakeCommandRunner().add_rule(
        "rev-parse", CommandResult(["git"], 0, "main\n", "")
    )
    assert GitRepo(runner).current_branch() == "main"


def test_has_changes_true_and_false():
    dirty = FakeCommandRunner().add_rule(
        "status", CommandResult(["git"], 0, " M a.py\n", "")
    )
    clean = FakeCommandRunner().add_rule(
        "status", CommandResult(["git"], 0, "", "")
    )
    assert GitRepo(dirty).has_changes() is True
    assert GitRepo(clean).has_changes() is False


def test_changed_files():
    runner = FakeCommandRunner().add_rule(
        "status", CommandResult(["git"], 0, " M a.py\n?? b.py\n\n", "")
    )
    assert GitRepo(runner).changed_files() == ["a.py", "b.py"]


def test_failed_command_raises():
    runner = FakeCommandRunner().add_rule(
        "commit", CommandResult(["git", "commit"], 1, "", "nothing to commit")
    )
    with pytest.raises(GitError, match="git commit"):
        GitRepo(runner).commit("msg")


def test_unchecked_command_does_not_raise():
    runner = FakeCommandRunner(default_exit_code=1)
    # _git with check=False should return the failed result rather than raise.
    result = GitRepo(runner)._git("status", check=False)
    assert result.exit_code == 1
