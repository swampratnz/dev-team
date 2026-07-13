"""Tests for side-effect policy and the guarded command runner."""

from __future__ import annotations

import pytest

from dev_team.approval import CallbackApprovalGate, DenyAll
from dev_team.execution import CommandResult, FakeCommandRunner
from dev_team.policy import (
    EXIT_DENIED,
    GuardedCommandRunner,
    SideEffectPolicy,
)


def test_policy_permits_normal_command():
    verdict = SideEffectPolicy().evaluate(["pytest", "-q"])
    assert verdict.allowed and not verdict.requires_approval


def test_policy_empty_command():
    assert SideEffectPolicy().evaluate([]).allowed is False


def test_policy_denies_dangerous_substring():
    verdict = SideEffectPolicy().evaluate(["bash", "-c", "rm -rf /"])
    assert verdict.allowed is False
    assert "blocked" in verdict.reason


def test_policy_allowlist():
    policy = SideEffectPolicy(allowed_programs=("git", "pytest"))
    assert policy.evaluate(["python", "x.py"]).allowed is False
    assert policy.evaluate(["git", "status"]).allowed is True


def test_policy_requires_approval_for_risky():
    verdict = SideEffectPolicy().evaluate(["git", "push", "origin", "main"])
    assert verdict.allowed and verdict.requires_approval


# --- denylist: argv-semantic destructive rm ----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ["rm", "-rf", "/"],
        ["rm", "-fr", "/"],  # flags in the other order
        ["rm", "-rf", "~"],  # target is not "/"
        ["rm", "-rf", "$HOME"],  # nor an unexpanded variable
        ["rm", "-r", "-f", "x"],  # separate short flags
        ["rm", "-Rf", "x"],  # capital -R is recursive too
        ["rm", "--recursive", "--force", "x"],  # long flags
        ["/usr/bin/rm", "-rf", "build"],  # program given by full path
    ],
)
def test_policy_blocks_every_spelling_of_destructive_rm(command):
    verdict = SideEffectPolicy().evaluate(command)
    assert verdict.allowed is False
    assert "blocked" in verdict.reason and "rm" in verdict.reason


def test_policy_allows_non_destructive_rm_but_gates_it():
    # rm without BOTH recursive and force is not the denied pattern; it is a
    # gated verb, so it is allowed-with-approval rather than blocked outright.
    for command in (["rm", "notes.txt"], ["rm", "-f", "notes.txt"], ["rm", "-r", "dir"]):
        verdict = SideEffectPolicy().evaluate(command)
        assert verdict.allowed is True
        assert verdict.requires_approval is True


def test_policy_bare_rm_is_allowed_with_approval():
    # rm with no arguments: not destructive (no flags), still a gated verb.
    verdict = SideEffectPolicy().evaluate(["rm"])
    assert verdict.allowed is True and verdict.requires_approval is True


# --- denylist: program token, not substring ----------------------------------------


def test_policy_does_not_treat_sudo_as_a_substring():
    # The classic false positive: a file whose name merely contains "sudo".
    verdict = SideEffectPolicy().evaluate(["cat", "sudoku.txt"])
    assert verdict.allowed is True and not verdict.requires_approval


def test_policy_blocks_privilege_escalation_programs():
    for program in ("sudo", "doas"):
        verdict = SideEffectPolicy().evaluate([program, "reboot"])
        assert verdict.allowed is False and "blocked" in verdict.reason


def test_policy_blocks_mkfs_family_as_program():
    verdict = SideEffectPolicy().evaluate(["mkfs.ext4", "/dev/sda1"])
    assert verdict.allowed is False and "blocked" in verdict.reason
    # ...but a file merely starting with "mkfs" as an argument is fine.
    ok = SideEffectPolicy().evaluate(["cat", "mkfs.notes"])
    assert ok.allowed is True


def test_policy_blocks_fork_bomb():
    verdict = SideEffectPolicy().evaluate(["bash", "-c", ":(){ :|:& };:"])
    assert verdict.allowed is False and "fork bomb" in verdict.reason


# --- approval gate: all tokens + shell wrappers ------------------------------------


def test_policy_gates_verb_hidden_behind_a_global_option():
    # push is argv[3] here, so the old argv[0]/argv[1] check missed it.
    verdict = SideEffectPolicy().evaluate(["git", "-C", "sub", "push"])
    assert verdict.allowed and verdict.requires_approval


def test_policy_gates_verb_inside_a_shell_wrapper():
    verdict = SideEffectPolicy().evaluate(["bash", "-c", "git push origin main"])
    assert verdict.allowed and verdict.requires_approval


def test_policy_denies_dangerous_program_inside_shell_wrapper():
    # A benign command in front of the destructive one must not mask it.
    verdict = SideEffectPolicy().evaluate(["bash", "-c", "cd repo && rm -rf /"])
    assert verdict.allowed is False and "blocked" in verdict.reason
    # sudo as the inner program is caught the same way.
    esc = SideEffectPolicy().evaluate(["sh", "-c", "sudo rm x"])
    assert esc.allowed is False and "blocked" in esc.reason


def test_policy_shell_wrapper_with_unbalanced_quotes_falls_back():
    # An unparseable script must not crash or fail open; the whitespace-split
    # fallback still surfaces the gated verb.
    verdict = SideEffectPolicy().evaluate(["bash", "-c", "git push 'oops"])
    assert verdict.allowed and verdict.requires_approval


def test_policy_shell_wrapper_edge_cases_are_harmless():
    # bash invoked without -c: no nested script to inspect.
    assert SideEffectPolicy().evaluate(["bash", "script.sh"]).allowed is True
    # bash -c with no following script argument.
    assert SideEffectPolicy().evaluate(["bash", "-c"]).allowed is True
    # bash -c with an empty script.
    assert SideEffectPolicy().evaluate(["bash", "-c", ""]).allowed is True


# --- user-supplied literal denials --------------------------------------------------


def test_policy_custom_denied_substring_blocks():
    policy = SideEffectPolicy(denied_substrings=("secret-host",))
    verdict = policy.evaluate(["curl", "https://secret-host/data"])
    assert verdict.allowed is False and "blocked" in verdict.reason


def test_policy_custom_denied_substring_absent_is_allowed():
    policy = SideEffectPolicy(denied_substrings=("secret-host",))
    assert policy.evaluate(["curl", "https://example.test/data"]).allowed is True


def test_guarded_runner_passes_normal_command():
    inner = FakeCommandRunner().add_rule("pytest", CommandResult(["pytest"], 0, "ok", ""))
    guarded = GuardedCommandRunner(inner)
    result = guarded.run(["pytest", "-q"])
    assert result.ok and result.stdout == "ok"


def test_guarded_runner_blocks_denied_command():
    guarded = GuardedCommandRunner(FakeCommandRunner())
    result = guarded.run(["sudo", "reboot"])
    assert result.exit_code == EXIT_DENIED
    assert "blocked" in result.stderr


def test_guarded_runner_approval_granted():
    inner = FakeCommandRunner()
    guarded = GuardedCommandRunner(
        inner, approval=CallbackApprovalGate(callback=lambda r: True)
    )
    result = guarded.run(["git", "push"])
    assert result.ok
    assert inner.calls == [["git", "push"]]


def test_guarded_runner_approval_denied():
    inner = FakeCommandRunner()
    guarded = GuardedCommandRunner(inner, approval=DenyAll())
    result = guarded.run(["git", "push"])
    assert result.exit_code == EXIT_DENIED
    assert "approval denied" in result.stderr
    assert inner.calls == []  # inner never ran


def test_guarded_runner_passes_env_through():
    inner = FakeCommandRunner()
    guarded = GuardedCommandRunner(inner)
    guarded.run(["git", "clone", "x"], env={"GIT_TERMINAL_PROMPT": "0"})
    assert inner.envs == [{"GIT_TERMINAL_PROMPT": "0"}]
