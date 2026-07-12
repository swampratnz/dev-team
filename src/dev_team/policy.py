"""Policy-as-code guardrails over side effects.

A :class:`SideEffectPolicy` decides whether a shell command is permitted, and
:class:`GuardedCommandRunner` enforces that policy (plus an approval gate) in
front of any real :class:`~dev_team.execution.CommandRunner`.

These guardrails are defence-in-depth, not a sandbox: any gate that *executes*
agent-authored code (e.g. running its tests) is arbitrary code execution, and
no argv-level policy can contain that. For untrusted or unattended runs, put
the whole workspace inside an isolated container or VM as well.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .approval import ApprovalGate, ApprovalRequest, AutoApprover
from .execution import CommandResult, CommandRunner

EXIT_DENIED = 126


@dataclass
class PolicyVerdict:
    """The result of evaluating a command against a policy."""

    allowed: bool
    reason: str
    requires_approval: bool = False


@dataclass
class SideEffectPolicy:
    """Allow/deny rules for shell commands.

    Attributes:
        allowed_programs: If non-empty, only commands whose program (argv[0])
            is in this set are allowed.
        denied_substrings: Any command whose joined form contains one of these
            is denied outright.
        approval_commands: Commands are allowed only after approval when the
            *command position* — the basename of argv[0] or the subcommand in
            argv[1] — equals one of these words (e.g. ``push``, ``deploy``,
            ``rm``). Position matters, not mere token presence: ``rm -rf x``
            and ``git push`` are gated, while ``git stash push`` (push as an
            argument to stash, run internally by the fail-to-pass check) and a
            file argument that happens to be called ``deploy`` are not.
    """

    allowed_programs: Sequence[str] = field(default_factory=tuple)
    denied_substrings: Sequence[str] = ("rm -rf /", "sudo", ":(){", "mkfs")
    approval_commands: Sequence[str] = ("push", "deploy", "rm")

    def evaluate(self, command: Sequence[str]) -> PolicyVerdict:
        """Evaluate ``command`` and return a :class:`PolicyVerdict`."""

        args = list(command)
        if not args:
            return PolicyVerdict(False, "empty command")
        joined = " ".join(args)

        for bad in self.denied_substrings:
            if bad in joined:
                return PolicyVerdict(False, f"blocked by policy: contains {bad!r}")

        if self.allowed_programs and args[0] not in self.allowed_programs:
            return PolicyVerdict(False, f"program not allow-listed: {args[0]!r}")

        tokens = {args[0].rsplit("/", 1)[-1]}
        if len(args) > 1:
            tokens.add(args[1])
        for risky in self.approval_commands:
            if risky in tokens:
                return PolicyVerdict(
                    True, f"requires approval: contains {risky!r}", requires_approval=True
                )

        return PolicyVerdict(True, "permitted")


@dataclass
class GuardedCommandRunner:
    """Wraps a :class:`CommandRunner`, enforcing a policy and approval gate."""

    inner: CommandRunner
    policy: SideEffectPolicy = field(default_factory=SideEffectPolicy)
    approval: ApprovalGate = field(default_factory=AutoApprover)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        args = list(command)
        verdict = self.policy.evaluate(args)
        if not verdict.allowed:
            return CommandResult(args, EXIT_DENIED, "", verdict.reason)
        if verdict.requires_approval:
            decision = self.approval.review(
                ApprovalRequest(action=" ".join(args), detail=verdict.reason, risk="high")
            )
            if not decision.approved:
                return CommandResult(
                    args, EXIT_DENIED, "", f"approval denied: {decision.reason}"
                )
        if env is not None:
            return self.inner.run(args, cwd=cwd, timeout=timeout, env=env)
        # Omit env entirely when unused so pre-env CommandRunner
        # implementations (user-supplied doubles included) keep working.
        return self.inner.run(args, cwd=cwd, timeout=timeout)
