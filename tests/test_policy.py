"""Tests for side-effect policy and the guarded command runner."""

from __future__ import annotations

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
