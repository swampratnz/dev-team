"""Container-backed command execution — real containment for untrusted code.

Running a project's own build/test commands, or an agent-authored test suite, is
arbitrary code execution on the host. The argv-level :class:`~dev_team.policy.
SideEffectPolicy` is defence-in-depth, not containment (see its module note):
nothing at the argv layer can stop code a gate *runs* from reading the host or
calling out to the network.

:class:`ContainerCommandRunner` closes that gap. It wraps another
:class:`~dev_team.execution.CommandRunner` and runs each command inside a
short-lived, rootless container with, by default, **no network**, **all Linux
capabilities dropped**, **no-new-privileges**, and **memory/CPU/PID limits** —
so the code the engine triggers (gates, build probes, setup and scan commands)
executes in a box whose only shared, writable surface is the workspace.

Three design choices make this both correct and testable:

- **git stays on the host.** git porcelain (commit/branch/stash/worktree/log) is
  orchestration the engine itself controls — not untrusted code — and it must act
  on the *real* repository and its `.git`. So any command whose program is in
  :attr:`SandboxConfig.host_programs` (``git`` by default) is delegated straight
  to the inner runner and never containerised.
- **execution is delegated, not performed.** The runner only *builds* the
  ``docker``/``podman`` argv and hands it to the injected inner runner. That
  keeps it fully unit-testable without a container engine (inject a fake), and it
  inherits the inner runner's timeout handling and secret-env scrubbing for free.
- **the workspace is the trust boundary.** ``cwd`` — which the engine always
  roots at the workspace (or a per-task worktree) — is the *only* host path
  bind-mounted into the container, and only the caller-supplied ``env`` is
  forwarded, never the host environment. That ``env`` rides in via a
  mode-``0600``, workspace-external, always-deleted ``--env-file`` — never inline
  ``--env KEY=VALUE`` — so a credential handed through it never lands in the
  container CLI's argv (``ps`` / ``CommandResult.command`` / an audit report) and
  the boxed code cannot read the file either.

What this does **not** contain: the agentic engineer's own SDK tool loop runs via
the Claude CLI on the host, outside any :class:`CommandRunner`, so containing
*that* means running the whole dev-team process inside a container/VM. This
primitive contains the code-execution surface the engine drives; process-level
isolation is a separate (deployment) concern.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .execution import CommandResult, CommandRunner
from .errors import DevTeamError


class SandboxError(DevTeamError):
    """A command could not be containerised (e.g. no ``cwd`` to mount)."""


#: Programs that are host orchestration, not untrusted code, and so run on the
#: host rather than in the sandbox. ``git`` manages the real repo/worktrees and
#: must never be boxed into an ephemeral, networkless container.
DEFAULT_HOST_PROGRAMS = frozenset({"git"})


@dataclass(frozen=True)
class SandboxConfig:
    """How to run a command inside a container.

    The defaults are the safe ones: ``network="none"`` (no exfiltration or
    dependency confusion), every capability dropped, no-new-privileges, and
    conservative resource ceilings. ``read_only_rootfs`` is opt-in because many
    toolchains write outside the workspace (caches, ``$HOME``); pair it with
    ``tmpfs`` (and a workspace ``$HOME``) when your stack tolerates it.

    ``user`` is left unset by default on purpose: the intended model is a
    **rootless** engine (rootless podman/docker), which already maps the
    container to the invoking host user, so workspace files are written back
    with correct ownership without a ``--user`` flag. Set it explicitly for a
    root-daemon docker.
    """

    #: Container CLI. ``podman`` is the rootless-by-default choice; ``docker``
    #: works too (prefer a rootless docker daemon).
    engine: str = "docker"
    #: Image the command runs in. There is no universally correct default — it
    #: must carry the toolchain the gate needs (python/node/dotnet/...). Callers
    #: should set this to match their stack; the default is a small Python base.
    image: str = "python:3.12-slim"
    #: ``docker run --network`` value. ``none`` is the secure default; a setup
    #: command that must fetch dependencies needs an explicit override.
    network: str = "none"
    #: ``--user`` value (e.g. ``"1000:1000"``). ``None`` omits the flag and
    #: relies on a rootless engine mapping to the host user.
    user: Optional[str] = None
    #: In-container path the workspace (``cwd``) is bind-mounted to.
    workspace_mount: str = "/workspace"
    #: ``--memory`` ceiling (e.g. ``"2g"``); ``None`` omits it.
    memory: Optional[str] = "2g"
    #: ``--cpus`` ceiling (e.g. ``"2"``); ``None`` omits it.
    cpus: Optional[str] = "2"
    #: ``--pids-limit`` (fork-bomb ceiling); ``None`` omits it.
    pids_limit: Optional[int] = 512
    #: ``--cap-drop ALL`` when True.
    drop_capabilities: bool = True
    #: ``--security-opt no-new-privileges`` when True.
    no_new_privileges: bool = True
    #: ``--read-only`` root filesystem when True (workspace mount stays writable).
    read_only_rootfs: bool = False
    #: Paths mounted as writable tmpfs (useful with ``read_only_rootfs``).
    tmpfs: Tuple[str, ...] = ()
    #: Programs delegated to the host instead of being containerised.
    host_programs: frozenset = DEFAULT_HOST_PROGRAMS
    #: Extra ``docker run`` arguments inserted verbatim before the image (an
    #: escape hatch for site-specific flags).
    extra_run_args: Tuple[str, ...] = ()


@dataclass
class ContainerCommandRunner:
    """A :class:`CommandRunner` that runs commands inside a container.

    Wraps ``inner`` (a real :class:`~dev_team.execution.SubprocessCommandRunner`
    in production): git-family commands are delegated to it unchanged, every
    other command is rewritten into a ``docker run`` invocation and *that* is
    handed to ``inner`` to execute.
    """

    inner: CommandRunner
    config: SandboxConfig = field(default_factory=SandboxConfig)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        args = list(command)
        if args and _program(args[0]) in self.config.host_programs:
            # Host orchestration (git): run outside the sandbox, unchanged.
            return self.inner.run(args, cwd=cwd, timeout=timeout, env=env)
        forwarded = _clean_env(env)
        if not forwarded:
            wrapped = self._containerise(args, cwd=cwd, env_file=None)
            return self.inner.run(wrapped, cwd=cwd, timeout=timeout)
        # Forwarded env goes via a mode-0600, workspace-external, always
        # cleaned-up --env-file, never inline --env KEY=VALUE: a secret handed
        # through env must not land in the container CLI's argv (which becomes
        # CommandResult.command, and from there `ps` and audit reports). The
        # file lives outside the bind-mount, so the boxed code can't read it.
        fd, env_path = tempfile.mkstemp(prefix="dev-team-sandbox-", suffix=".env")
        try:
            with os.fdopen(fd, "w") as handle:
                for key, value in forwarded.items():
                    handle.write(f"{key}={value}\n")
            wrapped = self._containerise(args, cwd=cwd, env_file=env_path)
            return self.inner.run(wrapped, cwd=cwd, timeout=timeout)
        finally:
            os.unlink(env_path)

    def _containerise(
        self,
        args: List[str],
        *,
        cwd: Optional[str],
        env_file: Optional[str],
    ) -> List[str]:
        """Build the ``docker run`` argv that runs ``args`` in a container."""

        if cwd is None:
            # Sandboxing needs a concrete directory to bind-mount as the
            # workspace; the engine always roots gate/probe commands at one.
            raise SandboxError(
                "cannot sandbox a command without a workspace directory to mount"
            )
        cfg = self.config
        host_dir = os.path.abspath(cwd)
        mount = cfg.workspace_mount
        run: List[str] = [cfg.engine, "run", "--rm"]
        run += ["--network", cfg.network]
        if cfg.user is not None:
            run += ["--user", cfg.user]
        if cfg.drop_capabilities:
            run += ["--cap-drop", "ALL"]
        if cfg.no_new_privileges:
            run += ["--security-opt", "no-new-privileges"]
        if cfg.read_only_rootfs:
            run.append("--read-only")
        for path in cfg.tmpfs:
            run += ["--tmpfs", path]
        if cfg.memory is not None:
            run += ["--memory", cfg.memory]
        if cfg.cpus is not None:
            run += ["--cpus", cfg.cpus]
        if cfg.pids_limit is not None:
            run += ["--pids-limit", str(cfg.pids_limit)]
        run += ["--volume", f"{host_dir}:{mount}"]
        run += ["--workdir", mount]
        if env_file is not None:
            run += ["--env-file", env_file]
        run += list(cfg.extra_run_args)
        run.append(cfg.image)
        run += args
        return run


def _program(arg0: str) -> str:
    """The bare program name of ``arg0`` (path- and suffix-stripped)."""

    # Strip any directory and a trailing .exe so "/usr/bin/git" and "git.exe"
    # both read as "git". Use PurePosixPath so a forward-slash path parses the
    # same on any host.
    name = PurePosixPath(arg0.replace("\\", "/")).name
    return name[:-4] if name.endswith(".exe") else name


def _clean_env(env: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Forwarded env for the container: caller-supplied entries only, sorted.

    Sorted for a deterministic argv (stable tests, stable logs). The caller
    owns what it passes; nothing is pulled from the host environment.
    """

    if not env:
        return {}
    return {key: env[key] for key in sorted(env)}
