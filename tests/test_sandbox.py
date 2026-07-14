"""Tests for the container-backed command runner (sandboxing).

Every case delegates real execution to a recording spy, so the suite exercises
argv construction and the git-on-host policy without a container engine.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Sequence, Tuple

import pytest

from dev_team.execution import CommandResult
from dev_team.sandbox import (
    ContainerCommandRunner,
    SandboxConfig,
    SandboxError,
)


@dataclass
class _Spy:
    """A CommandRunner that records every call and returns a canned result."""

    result: CommandResult = field(
        default_factory=lambda: CommandResult(["x"], 0, "out", "")
    )
    calls: List[Tuple[Sequence[str], Optional[str], Optional[float], Optional[Mapping[str, str]]]] = field(
        default_factory=list
    )

    def run(self, command, *, cwd=None, timeout=None, env=None) -> CommandResult:
        self.calls.append((list(command), cwd, timeout, env))
        return self.result


def _argv(spy: _Spy) -> List[str]:
    return list(spy.calls[-1][0])


# -- git-on-host delegation -------------------------------------------------


def test_git_is_delegated_to_the_host_unchanged():
    spy = _Spy()
    runner = ContainerCommandRunner(spy)
    env = {"GIT_CONFIG_COUNT": "1"}
    result = runner.run(["git", "status"], cwd="/ws", timeout=5.0, env=env)
    # Not containerised: exact argv, cwd, timeout and env pass straight through.
    assert spy.calls == [(["git", "status"], "/ws", 5.0, env)]
    assert result is spy.result


@pytest.mark.parametrize("arg0", ["git", "/usr/bin/git", "git.exe", "C:\\bin\\git.exe"])
def test_git_delegated_regardless_of_path_or_suffix(arg0):
    spy = _Spy()
    ContainerCommandRunner(spy).run([arg0, "log"], cwd="/ws")
    assert _argv(spy) == [arg0, "log"]  # delegated, not wrapped in docker


def test_custom_host_program_is_delegated():
    spy = _Spy()
    cfg = SandboxConfig(host_programs=frozenset({"git", "make"}))
    ContainerCommandRunner(spy, cfg).run(["make", "build"], cwd="/ws")
    assert _argv(spy) == ["make", "build"]


# -- containerised commands -------------------------------------------------


def test_non_git_command_is_wrapped_in_docker_run():
    spy = _Spy()
    result = ContainerCommandRunner(spy).run(["pytest", "-q"], cwd="/ws", timeout=9.0)
    argv = _argv(spy)
    assert argv[:3] == ["docker", "run", "--rm"]
    # secure defaults present
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--memory") + 1] == "2g"
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    # only the workspace is mounted, and the command runs there
    assert argv[argv.index("--volume") + 1] == "/ws:/workspace"
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    # the image precedes the actual command
    assert argv[-3:] == ["python:3.12-slim", "pytest", "-q"]
    # the container CLI runs from cwd with the host timeout; the app env is NOT
    # forwarded to the CLI process (it rides in via -e instead)
    assert spy.calls[-1][1] == "/ws"
    assert spy.calls[-1][2] == 9.0
    assert spy.calls[-1][3] is None
    assert result is spy.result


def test_no_cwd_to_mount_is_an_error():
    with pytest.raises(SandboxError):
        ContainerCommandRunner(_Spy()).run(["pytest"], cwd=None)


def test_relative_cwd_is_resolved_for_the_mount():
    spy = _Spy()
    ContainerCommandRunner(spy).run(["pytest"], cwd="build")
    argv = _argv(spy)
    # realpath-resolved (absolute, symlink-free) mount source
    expected = f"{os.path.realpath('build')}:/workspace"
    assert argv[argv.index("--volume") + 1] == expected


def test_user_flag_added_only_when_set():
    plain = _Spy()
    ContainerCommandRunner(plain).run(["pytest"], cwd="/ws")
    assert "--user" not in _argv(plain)

    with_user = _Spy()
    ContainerCommandRunner(with_user, SandboxConfig(user="1000:1000")).run(
        ["pytest"], cwd="/ws"
    )
    argv = _argv(with_user)
    assert argv[argv.index("--user") + 1] == "1000:1000"


def test_hardening_flags_can_be_disabled():
    spy = _Spy()
    cfg = SandboxConfig(
        drop_capabilities=False,
        no_new_privileges=False,
        memory=None,
        cpus=None,
        pids_limit=None,
    )
    ContainerCommandRunner(spy, cfg).run(["pytest"], cwd="/ws")
    argv = _argv(spy)
    assert "--cap-drop" not in argv
    assert "no-new-privileges" not in argv
    assert "--memory" not in argv
    assert "--cpus" not in argv
    assert "--pids-limit" not in argv


def test_read_only_rootfs_and_tmpfs():
    spy = _Spy()
    cfg = SandboxConfig(read_only_rootfs=True, tmpfs=("/tmp", "/run"))
    ContainerCommandRunner(spy, cfg).run(["pytest"], cwd="/ws")
    argv = _argv(spy)
    assert "--read-only" in argv
    tmpfs_values = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs"]
    assert tmpfs_values == ["/tmp", "/run"]


def test_read_only_omitted_by_default():
    spy = _Spy()
    ContainerCommandRunner(spy).run(["pytest"], cwd="/ws")
    argv = _argv(spy)
    assert "--read-only" not in argv
    assert "--tmpfs" not in argv


def test_env_is_forwarded_via_a_secure_env_file_not_argv():
    # A secret handed through env must reach the container as real env, never as
    # argv (ps / CommandResult.command / an audit report). It rides in via a
    # mode-0600, workspace-external, cleaned-up --env-file.
    captured = {}

    class _EnvSpy:
        result = CommandResult(["x"], 0, "", "")

        def run(self, command, *, cwd=None, timeout=None, env=None):
            argv = list(command)
            path = argv[argv.index("--env-file") + 1]
            captured["argv"] = argv
            captured["path"] = path
            captured["mode"] = stat.S_IMODE(os.stat(path).st_mode)
            with open(path, encoding="utf-8") as handle:
                captured["content"] = handle.read()
            return self.result

    ContainerCommandRunner(_EnvSpy()).run(
        ["pytest"], cwd="/ws", env={"B": "2", "TOKEN": "s3cr3t", "A": "1"}
    )
    # the secret never appears in the argv, and nothing is inlined as --env
    assert not any("s3cr3t" in a for a in captured["argv"])
    assert "--env" not in captured["argv"]
    # the env-file is 0600, lives outside the mounted workspace, and holds the
    # sorted entries
    assert captured["mode"] == 0o600
    assert not captured["path"].startswith("/ws")
    assert captured["content"] == "A=1\nB=2\nTOKEN=s3cr3t\n"
    # ...and it is deleted after the run
    assert not os.path.exists(captured["path"])


def test_env_file_is_cleaned_up_even_when_the_command_raises():
    seen = {}

    class _RaisingSpy:
        def run(self, command, *, cwd=None, timeout=None, env=None):
            argv = list(command)
            seen["path"] = argv[argv.index("--env-file") + 1]
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        ContainerCommandRunner(_RaisingSpy()).run(
            ["pytest"], cwd="/ws", env={"A": "1"}
        )
    assert not os.path.exists(seen["path"])


def test_no_env_means_no_env_file():
    spy = _Spy()
    ContainerCommandRunner(spy).run(["pytest"], cwd="/ws")
    argv = _argv(spy)
    assert "--env-file" not in argv
    assert "--env" not in argv


def test_custom_engine_image_mount_and_extra_args():
    spy = _Spy()
    cfg = SandboxConfig(
        engine="podman",
        image="node:22",
        workspace_mount="/src",
        network="bridge",
        extra_run_args=("--dns", "10.0.0.1"),
    )
    ContainerCommandRunner(spy, cfg).run(["npm", "test"], cwd="/ws")
    argv = _argv(spy)
    assert argv[0] == "podman"
    assert argv[argv.index("--network") + 1] == "bridge"
    assert argv[argv.index("--volume") + 1] == "/ws:/src"
    assert argv[argv.index("--workdir") + 1] == "/src"
    # extra args sit immediately before the image
    image_at = argv.index("node:22")
    assert argv[image_at - 2 : image_at] == ["--dns", "10.0.0.1"]
    assert argv[image_at + 1 :] == ["npm", "test"]


def test_empty_command_runs_the_image_default():
    # A falsy command skips the git check and still produces a valid run argv
    # (the image's default entrypoint); mostly a guard against an IndexError.
    spy = _Spy()
    ContainerCommandRunner(spy).run([], cwd="/ws")
    argv = _argv(spy)
    assert argv[-1] == "python:3.12-slim"  # nothing appended after the image
