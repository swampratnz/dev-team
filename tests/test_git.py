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
    # git status -z is NUL-separated (each record terminated by NUL), so the
    # fake emits that shape rather than newline-delimited lines.
    runner = FakeCommandRunner().add_rule(
        "status --porcelain -uall -z",
        CommandResult(["git"], 0, " M a.py\x00?? b.py\x00", ""),
    )
    assert GitRepo(runner).changed_files() == ["a.py", "b.py"]
    # the -z flag must actually be issued, else paths would come back C-quoted
    assert ["git", "status", "--porcelain", "-uall", "-z"] in runner.calls


def test_changed_files_returns_spaced_and_non_ascii_paths_unquoted():
    # Under -z, paths with spaces or non-ASCII bytes are emitted verbatim
    # (not C-quoted), so they feed straight back into ``git add`` unchanged.
    runner = FakeCommandRunner().add_rule(
        "status --porcelain -uall -z",
        CommandResult(["git"], 0, "?? my file.py\x00?? café.py\x00", ""),
    )
    assert GitRepo(runner).changed_files() == ["my file.py", "café.py"]


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
    # with no untracked files, no intent-to-add dance happens
    assert not any(c[:3] == ["git", "add", "-N"] for c in cmd.calls)


def test_diff_includes_untracked_via_intent_to_add_and_restores_index():
    # ``git diff HEAD`` alone omits untracked files; diff() marks them
    # intent-to-add so they appear, then un-adds them to leave the index as
    # it was. The blank line in the ls-files output must be ignored.
    cmd = FakeCommandRunner()
    cmd.add_rule(
        "ls-files --others --exclude-standard",
        CommandResult(["git"], 0, "new.py\n\nsub/added.py\n", ""),
    )
    cmd.add_rule("diff HEAD", CommandResult(["git"], 0, "the-patch", ""))
    assert GitRepo(cmd).diff() == "the-patch"
    # untracked files were marked intent-to-add so they show up in the diff...
    assert ["git", "add", "-N", "--", "new.py", "sub/added.py"] in cmd.calls
    # ...then un-added so the index round-trips to exactly its prior state
    assert ["git", "reset", "--", "new.py", "sub/added.py"] in cmd.calls


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
    # Under -z a rename is "R  <new>\0<old>\0": the new path rides the status
    # record and the old path follows in the next NUL field (order reversed
    # from the non-z "old -> new" form). The old field must be consumed.
    cmd = FakeCommandRunner()
    cmd.add_rule(
        "status --porcelain -uall -z",
        CommandResult(["git"], 0, "R  new.py\x00old.py\x00?? sub/added.py\x00", ""),
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


def test_stash_push_reports_no_entry_on_exit_zero_with_nothing_to_save():
    # git exits 0 with "No local changes to save" when the pathspec matched
    # nothing to shelve — no entry was created, so a pop would restore an
    # unrelated older stash. The push must report failure despite exit 0.
    cmd = FakeCommandRunner()
    cmd.add_rule(
        "stash push", CommandResult(["git"], 0, "No local changes to save", "")
    )
    assert GitRepo(cmd).stash_push(["a.py"]) is False


def test_stash_pop_reports_success_and_conflict():
    ok = FakeCommandRunner()  # default exit 0
    assert GitRepo(ok).stash_pop() is True
    conflicting = FakeCommandRunner().add_rule(
        "stash pop", CommandResult(["git"], 1, "", "CONFLICT (content): merge conflict")
    )
    assert GitRepo(conflicting).stash_pop() is False


def test_reset_hard_moves_tip_to_ref():
    cmd = FakeCommandRunner()
    GitRepo(cmd).reset_hard("abc123")
    assert ["git", "reset", "--hard", "abc123"] in cmd.calls


def test_commit_allow_empty():
    cmd = FakeCommandRunner()
    GitRepo(cmd).commit("msg", allow_empty=True)
    assert ["git", "commit", "--allow-empty", "-m", "msg"] in cmd.calls


def test_push_sets_upstream_and_threads_env():
    # The credential rides in env (the http.extraheader GIT_CONFIG_* the
    # sources module builds), never in argv — so it stays out of process
    # listings and .git/config. set_upstream adds --set-upstream.
    cmd = FakeCommandRunner()
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader"}
    GitRepo(cmd).push("dev-team/feat", set_upstream=True, env=env)
    assert cmd.calls[-1] == ["git", "push", "--set-upstream", "origin", "dev-team/feat"]
    assert cmd.envs[-1] == env


def test_push_force_with_lease_and_custom_remote():
    # force_with_lease (never a bare --force) is the safe re-push form; a
    # custom remote is honoured. No env is fine (public push).
    cmd = FakeCommandRunner()
    GitRepo(cmd).push("main", remote="upstream", force_with_lease=True)
    assert cmd.calls[-1] == ["git", "push", "--force-with-lease", "upstream", "main"]
    assert cmd.envs[-1] is None


def test_push_defaults_to_plain_push_to_origin():
    cmd = FakeCommandRunner()
    GitRepo(cmd).push("dev-team/feat")
    assert cmd.calls[-1] == ["git", "push", "origin", "dev-team/feat"]


def test_push_raises_when_rejected():
    cmd = FakeCommandRunner().add_rule(
        "push", CommandResult(["git", "push"], 1, "", "! [rejected] (fetch first)")
    )
    with pytest.raises(GitError, match="git push"):
        GitRepo(cmd).push("dev-team/feat")


def test_push_scrubs_credential_from_rejection_output():
    # A verbose/GIT_TRACE run can echo the AUTHORIZATION: basic <base64> header
    # (the credential push carries in env) back into git's output. The scrub
    # redactor the token-owning caller supplies must strip it before it reaches
    # the GitError, so a credential can never land in an error message — and
    # from there in the event log, transcript, or an outcome report.
    secret = "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46Z2hwX3NlY3JldA=="
    cmd = FakeCommandRunner().add_rule(
        "push",
        CommandResult(["git", "push"], 128, "", f"fatal: rejected; sent header {secret}"),
    )
    with pytest.raises(GitError) as exc:
        GitRepo(cmd).push(
            "dev-team/feat",
            env={"GIT_CONFIG_VALUE_0": secret},
            scrub=lambda text: text.replace(secret, "***"),
        )
    msg = str(exc.value)
    assert secret not in msg and "***" in msg
