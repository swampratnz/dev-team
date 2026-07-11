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


def test_is_repo_true_and_false():
    cmd = FakeCommandRunner()
    cmd.add_rule("rev-parse --is-inside-work-tree", CommandResult(["git"], 0, "true\n", ""))
    assert GitRepo(cmd).is_repo() is True
    assert GitRepo(FakeCommandRunner()).is_repo() is False


def test_ensure_repo_initialises_and_sets_identity():
    cmd = FakeCommandRunner()
    GitRepo(cmd).ensure_repo()
    assert ["git", "init"] in cmd.calls
    assert ["git", "config", "user.name", "dev-team"] in cmd.calls
    assert ["git", "config", "user.email", "dev-team@localhost"] in cmd.calls


def test_ensure_repo_skips_existing_repo_and_identity():
    cmd = FakeCommandRunner()
    cmd.add_rule("rev-parse --is-inside-work-tree", CommandResult(["git"], 0, "true", ""))
    cmd.add_rule("config user.name", CommandResult(["git"], 0, "someone", ""))
    GitRepo(cmd).ensure_repo()
    assert ["git", "init"] not in cmd.calls
    # name already configured -> untouched; email missing -> set
    assert ["git", "config", "user.name", "dev-team"] not in cmd.calls
    assert ["git", "config", "user.email", "dev-team@localhost"] in cmd.calls


def test_diff_returns_patch():
    cmd = FakeCommandRunner()
    cmd.add_rule("diff HEAD", CommandResult(["git"], 0, "the-patch", ""))
    assert GitRepo(cmd).diff() == "the-patch"


def test_discard_changes_resets_and_cleans():
    cmd = FakeCommandRunner()
    GitRepo(cmd).discard_changes()
    assert ["git", "reset", "--hard"] in cmd.calls
    assert ["git", "clean", "-fd"] in cmd.calls


def test_switch_to_creates_new_branch():
    cmd = FakeCommandRunner()
    GitRepo(cmd).switch_to("dev-team/x")
    assert ["git", "checkout", "-b", "dev-team/x"] in cmd.calls


def test_switch_to_falls_back_to_existing_branch():
    cmd = FakeCommandRunner()
    cmd.add_rule("checkout -b", CommandResult(["git"], 1, "", "already exists"))
    GitRepo(cmd).switch_to("dev-team/x")
    assert ["git", "checkout", "dev-team/x"] in cmd.calls


def test_add_paths_skips_empty_list():
    cmd = FakeCommandRunner()
    GitRepo(cmd).add_paths([])
    assert cmd.calls == []


def test_changed_files_expands_untracked_and_renames():
    cmd = FakeCommandRunner()
    cmd.add_rule(
        "status --porcelain -uall",
        CommandResult(["git"], 0, "R  old.py -> new.py\n?? sub/added.py\n\n", ""),
    )
    assert GitRepo(cmd).changed_files() == ["new.py", "sub/added.py"]


def test_stash_push_and_pop():
    cmd = FakeCommandRunner()
    repo = GitRepo(cmd)
    assert repo.stash_push(["src/x.py"]) is True
    repo.stash_pop()
    assert ["git", "stash", "push", "-u", "--", "src/x.py"] in cmd.calls
    assert ["git", "stash", "pop"] in cmd.calls


def test_stash_push_reports_failure():
    cmd = FakeCommandRunner()
    cmd.add_rule("stash push", CommandResult(["git"], 1, "", "nothing to stash"))
    assert GitRepo(cmd).stash_push(["a.py"]) is False


def test_commit_allow_empty():
    cmd = FakeCommandRunner()
    GitRepo(cmd).commit("msg", allow_empty=True)
    assert ["git", "commit", "--allow-empty", "-m", "msg"] in cmd.calls
