"""Integration tests for the git porcelain against the real ``git`` binary.

The unit tests in ``test_git.py`` exercise :class:`GitRepo` over a
:class:`FakeCommandRunner`; these run every operation for real in a throwaway
repository under ``tmp_path``. They are hermetic: global and system git config
are masked so only the local identity written by ``ensure_repo`` applies.
"""

from __future__ import annotations

import os

import pytest

from dev_team.execution import SubprocessCommandRunner
from dev_team.git import GitRepo


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    """Mask global/system git config so tests never depend on the host."""

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture
def repo(tmp_path):
    """A :class:`GitRepo` over the real ``git`` binary in a fresh directory."""

    return GitRepo(SubprocessCommandRunner(), cwd=str(tmp_path))


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit_file(repo, tmp_path, name, content, message="commit"):
    _write(tmp_path, name, content)
    repo.add_paths([name])
    repo.commit(message)


def test_ensure_repo_initialises_fresh_dir_with_identity(repo):
    assert repo.is_repo() is False
    repo.ensure_repo()
    assert repo.is_repo() is True
    # commits must work on a fresh machine: a local identity was configured
    assert repo._git("config", "user.name").stdout.strip() == "dev-team"
    assert repo._git("config", "user.email").stdout.strip() == "dev-team@localhost"


def test_ensure_repo_is_idempotent_and_keeps_existing_identity(repo):
    repo.ensure_repo()
    repo._git("config", "user.name", "someone")
    repo.ensure_repo()
    assert repo._git("config", "user.name").stdout.strip() == "someone"


def test_add_commit_has_commits_and_rev_parse(repo, tmp_path):
    repo.ensure_repo()
    assert repo.has_commits() is False
    _commit_file(repo, tmp_path, "a.txt", "hello\n", message="init")
    assert repo.has_commits() is True
    sha = repo.rev_parse("HEAD")
    assert len(sha) == 40 and set(sha) <= set("0123456789abcdef")


def test_create_branch_and_switch_to_fallback(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "hello\n")
    base = repo.current_branch()
    repo.create_branch("feature")
    assert repo.current_branch() == "feature"
    repo.checkout(base)
    assert repo.current_branch() == base
    # switch_to falls back to a plain checkout when the branch already exists
    repo.switch_to("feature")
    assert repo.current_branch() == "feature"
    # ... and creates the branch when it does not
    repo.switch_to("another")
    assert repo.current_branch() == "another"


def test_has_changes_and_changed_files(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "old.txt", "same content\n")
    assert repo.has_changes() is False
    # a rename (staged, so status reports "R old.txt -> new.txt")
    (tmp_path / "old.txt").rename(tmp_path / "new.txt")
    repo.add_paths(["old.txt", "new.txt"])
    # an untracked file inside a new directory (needs -uall to be listed)
    _write(tmp_path, "pkg/inner.txt", "x\n")
    assert repo.has_changes() is True
    files = repo.changed_files()
    assert "new.txt" in files  # the rename reports the new path
    assert "old.txt" not in files
    assert "pkg/inner.txt" in files


def test_changed_files_handles_spaces_and_non_ascii_and_stages(repo, tmp_path):
    # Regression: the old newline-parsed ``git status --porcelain`` C-quoted
    # these names, so the quoted strings failed to stage (exit 128). With
    # ``-z`` the names come back verbatim and round-trip into ``git add``.
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "seed.txt", "seed\n")
    _write(tmp_path, "my file.txt", "spaces\n")
    _write(tmp_path, "café.txt", "unicode\n")
    files = repo.changed_files()
    assert "my file.txt" in files
    assert "café.txt" in files
    repo.add_paths(files)
    repo.commit("add tricky names")
    assert repo.has_changes() is False


def test_add_all_stages_everything(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "one\n")
    _write(tmp_path, "b.txt", "two\n")
    repo.add_all()
    repo.commit("add b")
    assert repo.has_changes() is False


def test_diff_reports_staged_and_unstaged_edits(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "before\n")
    _write(tmp_path, "a.txt", "after\n")
    diff = repo.diff()
    assert "-before" in diff
    assert "+after" in diff


def test_diff_includes_untracked_file_and_leaves_index_unchanged(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "committed\n")
    # a brand-new file git is not yet tracking
    _write(tmp_path, "brand_new.txt", "hello new file\n")
    diff = repo.diff()
    # the untracked file (and its content) appears in the reviewer's diff...
    assert "brand_new.txt" in diff
    assert "+hello new file" in diff
    # ...but the intent-to-add was undone: nothing is staged and the file is
    # still untracked, so the index round-tripped to exactly its prior state.
    assert repo._git("diff", "--cached", "--name-only").stdout.strip() == ""
    assert "?? brand_new.txt" in repo._git("status", "--porcelain").stdout


def test_reset_hard_restores_tip_to_captured_sha(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "one\n")
    head = repo.rev_parse("HEAD")
    _commit_file(repo, tmp_path, "b.txt", "two\n", message="second")
    assert repo.rev_parse("HEAD") != head
    # reset_hard moves the tip back to the captured sha, dropping the later
    # commit and its file entirely (the undo for a stray reset_soft).
    repo.reset_hard(head)
    assert repo.rev_parse("HEAD") == head
    assert not (tmp_path / "b.txt").exists()
    assert repo.has_changes() is False


def test_discard_changes_resets_and_cleans(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "keep\n")
    _write(tmp_path, "a.txt", "clobbered\n")
    _write(tmp_path, "junk/untracked.txt", "x\n")
    repo.discard_changes()
    assert (tmp_path / "a.txt").read_text() == "keep\n"
    assert not (tmp_path / "junk").exists()
    assert repo.has_changes() is False


def test_stash_push_pop_round_trip(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "committed\n")
    _write(tmp_path, "a.txt", "in flight\n")
    assert repo.stash_push(["a.txt"]) is True
    assert (tmp_path / "a.txt").read_text() == "committed\n"
    assert repo.has_changes() is False
    repo.stash_pop()
    assert (tmp_path / "a.txt").read_text() == "in flight\n"
    assert repo.has_changes() is True


def test_merge_squash_and_reset_soft(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "base\n")
    base_branch = repo.current_branch()
    base_sha = repo.rev_parse("HEAD")
    repo.create_branch("feature")
    _commit_file(repo, tmp_path, "feature.txt", "work\n", message="feature work")
    repo.checkout(base_branch)
    repo.merge_squash("feature")
    # the branch's changes are staged on the base branch, not committed
    assert repo.rev_parse("HEAD") == base_sha
    assert (tmp_path / "feature.txt").read_text() == "work\n"
    repo.commit("squashed")
    repo.reset_soft(base_sha)
    assert repo.rev_parse("HEAD") == base_sha
    assert repo.has_changes() is True  # the squashed work is staged again


def test_worktree_add_remove_and_delete_branch(repo, tmp_path):
    repo.ensure_repo()
    _commit_file(repo, tmp_path, "a.txt", "base\n")
    wt_path = tmp_path / "worktrees" / "wt1"
    repo.worktree_add(str(wt_path), "task/wt1")
    assert (wt_path / "a.txt").read_text() == "base\n"
    assert repo.rev_parse("task/wt1") == repo.rev_parse("HEAD")
    repo.worktree_remove(str(wt_path))
    assert not wt_path.exists()
    repo.delete_branch("task/wt1")
    assert not repo._git("rev-parse", "--verify", "task/wt1", check=False).ok
