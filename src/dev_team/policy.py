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

import re
import shlex
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Sequence

from .approval import ApprovalGate, ApprovalRequest, AutoApprover
from .execution import CommandResult, CommandRunner

EXIT_DENIED = 126

#: Shell programs whose ``-c SCRIPT`` argument is itself a command line. When a
#: command is one of these, the script is parsed and re-checked with the same
#: argv-level rules, so a dangerous program cannot hide one interpreter down
#: inside ``bash -c "..."``.
_SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})

#: Tokens that separate one command from the next inside a ``-c`` script.
#: shlex (with ``punctuation_chars``) emits these as standalone tokens; we
#: split on them so ``cd x && rm -rf /`` is seen as two commands and the
#: destructive ``rm`` is not masked by the harmless ``cd`` in front of it.
_SHELL_OPERATORS = frozenset({";", "&", "&&", "|", "||", "(", ")", "\n"})

#: Signature of the classic fork bomb ``:(){ :|:& };:`` — a shell function
#: named ``:`` that recursively forks itself. Distinctive enough to match
#: literally without risking a false positive on a real file name.
_FORK_BOMB = ":(){"


def _basename(program: str) -> str:
    """The bare program name without any directory (``/usr/bin/rm`` → ``rm``)."""

    # Split on both separators so a Windows-style path is decomposed too.
    return re.split(r"[\\/]", program)[-1]


def _shell_c_scripts(args: Sequence[str]) -> List[str]:
    """The script strings of a ``sh -c`` / ``bash -c`` invocation, if any.

    Only shell wrappers (:data:`_SHELL_WRAPPERS`) are considered, and a short
    option cluster containing ``c`` (``-c``, ``-lc``, …) is what marks the
    following argument as a command string. Anything else returns no scripts.
    """

    if not args or _basename(args[0]) not in _SHELL_WRAPPERS:
        return []
    scripts: List[str] = []
    index = 1
    while index < len(args):
        token = args[index]
        is_command_flag = (
            token.startswith("-")
            and not token.startswith("--")
            and "c" in token[1:]
        )
        if is_command_flag:
            if index + 1 < len(args):
                scripts.append(args[index + 1])
            index += 2
            continue
        index += 1
    return scripts


def _split_shell_script(script: str) -> List[List[str]]:
    """Tokenise a shell ``-c`` script into its constituent command argvs.

    The script is lexed with shell punctuation recognised, then split on the
    control operators in :data:`_SHELL_OPERATORS` so each sub-command's argv
    is returned separately. Unparseable quoting falls back to a plain
    whitespace split rather than silently skipping the script — detection must
    never fail open.
    """

    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = script.split()
    commands: List[List[str]] = [[]]
    for token in tokens:
        if token in _SHELL_OPERATORS:
            commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command]


def _is_destructive_rm(argv: Sequence[str]) -> bool:
    """Whether ``argv`` is an ``rm`` deleting recursively *and* forcibly.

    The program is matched by basename, and the recursive/force flags are
    accumulated across every option token, so ``rm -rf x``, ``rm -fr x``,
    ``rm -r -f x`` and ``rm --recursive --force x`` all match regardless of
    the target. The target is never inspected — ``rm -rf /``, ``rm -rf ~`` and
    ``rm -rf $HOME`` are equally destructive.
    """

    if _basename(argv[0]) != "rm":
        return False
    recursive = False
    force = False
    for token in argv[1:]:
        if token == "--recursive":
            recursive = True
        elif token == "--force":
            force = True
        elif token.startswith("-") and not token.startswith("--"):
            # A combined short-flag cluster such as -rf / -fr / -rfv.
            recursive = recursive or "r" in token or "R" in token
            force = force or "f" in token
    return recursive and force


def _denylist_reason(argv: Sequence[str]) -> Optional[str]:
    """Why ``argv`` is outright denied, or ``None`` if it is not.

    Matching is by argv semantics, never raw substring: the program is
    identified by its basename, so ``cat sudoku.txt`` (a file that merely
    contains ``sudo``) is untouched while ``sudo`` *as the program* is blocked.
    Callers pass only non-empty argvs.
    """

    program = _basename(argv[0])
    if _is_destructive_rm(argv):
        return "blocked by policy: recursive-force rm is destructive"
    if program in ("sudo", "doas"):
        return f"blocked by policy: privilege escalation via {program!r}"
    if program.startswith("mkfs"):
        return f"blocked by policy: filesystem format via {program!r}"
    return None


def _first_gated_verb(
    command_lines: Sequence[Sequence[str]], approval_commands: Sequence[str]
) -> Optional[str]:
    """The first gated verb appearing anywhere in the commands, or ``None``.

    Every token of every command line is considered — including the basename
    of each program and the tokens of a nested ``-c`` script — so a gated verb
    cannot escape the gate by hiding behind a global option (``git -C x
    push``) or inside a shell wrapper (``bash -c 'git push'``).
    """

    seen = set()
    for line in command_lines:
        for index, token in enumerate(line):
            seen.add(token)
            if index == 0:
                seen.add(_basename(token))
    for verb in approval_commands:
        if verb in seen:
            return verb
    return None


@dataclass
class PolicyVerdict:
    """The result of evaluating a command against a policy."""

    allowed: bool
    reason: str
    requires_approval: bool = False


@dataclass
class SideEffectPolicy:
    """Allow/deny rules for shell commands, matched by argv semantics.

    The built-in denials (a recursive-force ``rm``, ``sudo``/``doas``,
    ``mkfs*``, and the fork bomb) are recognised structurally — by program
    basename and flag parsing — not by raw substring, so ``cat sudoku.txt``
    is allowed while ``sudo reboot`` is not, and ``rm -fr /`` / ``rm -rf ~`` /
    ``rm -rf $HOME`` are all caught. When a command is a shell wrapper
    (``bash -c "..."``), its script is parsed and the same rules apply to each
    sub-command inside it.

    Attributes:
        allowed_programs: If non-empty, only commands whose program (argv[0])
            is in this set are allowed.
        denied_substrings: Extra, user-supplied literal substrings that deny a
            command outright. Empty by default — the built-in dangerous
            patterns are handled structurally above, precisely so a substring
            match cannot misfire on an innocent file name.
        approval_commands: A command needs approval when any of its tokens —
            across the whole argv (not just argv[0]/argv[1]) and any nested
            ``-c`` script — equals one of these verbs (e.g. ``push``,
            ``deploy``, ``rm``). Scanning every token is deliberate: it closes
            the ``git -C x push`` and ``bash -c 'git push'`` gaps. It also
            gates a few benign cases (``git stash push``, a file named
            ``deploy``); with the default :class:`~dev_team.approval.AutoApprover`
            those are recorded and auto-approved, and a stricter gate is where
            the extra caution is wanted.
    """

    allowed_programs: Sequence[str] = field(default_factory=tuple)
    denied_substrings: Sequence[str] = ()
    approval_commands: Sequence[str] = ("push", "deploy", "rm")

    def evaluate(self, command: Sequence[str]) -> PolicyVerdict:
        """Evaluate ``command`` and return a :class:`PolicyVerdict`."""

        args = list(command)
        if not args:
            return PolicyVerdict(False, "empty command")
        joined = " ".join(args)

        # The command itself plus each sub-command of any nested ``-c`` script,
        # so the argv-level checks below see what the shell would really run.
        command_lines: List[List[str]] = [args]
        for script in _shell_c_scripts(args):
            command_lines.extend(_split_shell_script(script))

        # The fork bomb is a shell construct, not a program; its signature
        # appears verbatim in the joined command (the ``-c`` script arg is part
        # of argv), so a single literal check covers both forms.
        if _FORK_BOMB in joined:
            return PolicyVerdict(False, "blocked by policy: fork bomb")

        for line in command_lines:
            denied = _denylist_reason(line)
            if denied is not None:
                return PolicyVerdict(False, denied)

        for bad in self.denied_substrings:
            if bad in joined:
                return PolicyVerdict(False, f"blocked by policy: contains {bad!r}")

        if self.allowed_programs and args[0] not in self.allowed_programs:
            return PolicyVerdict(False, f"program not allow-listed: {args[0]!r}")

        gated = _first_gated_verb(command_lines, self.approval_commands)
        if gated is not None:
            return PolicyVerdict(
                True,
                f"requires approval: contains {gated!r}",
                requires_approval=True,
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
