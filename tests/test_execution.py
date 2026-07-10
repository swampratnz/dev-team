"""Tests for the workspace and command execution layer."""

from __future__ import annotations

import subprocess
import sys

import pytest

from dev_team.execution import (
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    CommandResult,
    FakeCommandRunner,
    InMemoryWorkspace,
    LocalWorkspace,
    SubprocessCommandRunner,
    Workspace,
    WorkspaceError,
    _normalise,
)


# --- path normalisation -------------------------------------------------


def test_normalise_collapses_and_cleans():
    assert _normalise("./a/./b") == "a/b"
    assert _normalise("a//b/") == "a/b"


@pytest.mark.parametrize("bad", ["/abs", "../escape", "a/../../b", "", ".", "/"])
def test_normalise_rejects_bad_paths(bad):
    with pytest.raises(WorkspaceError):
        _normalise(bad)


# --- InMemoryWorkspace --------------------------------------------------


def test_in_memory_roundtrip():
    ws = InMemoryWorkspace({"a.txt": "hello"})
    assert isinstance(ws, Workspace)
    assert ws.read_text("a.txt") == "hello"
    ws.write_text("dir/b.txt", "x")
    assert ws.exists("dir/b.txt")
    assert ws.list_files() == ["a.txt", "dir/b.txt"]
    ws.delete("a.txt")
    assert not ws.exists("a.txt")
    # deleting a missing file is a no-op
    ws.delete("nope.txt")


def test_in_memory_missing_read_raises():
    ws = InMemoryWorkspace()
    with pytest.raises(WorkspaceError):
        ws.read_text("missing.txt")


# --- LocalWorkspace -----------------------------------------------------


def test_local_workspace_roundtrip(tmp_path):
    ws = LocalWorkspace(str(tmp_path / "root"))
    ws.write_text("pkg/mod.py", "code")
    assert ws.exists("pkg/mod.py")
    assert ws.read_text("pkg/mod.py") == "code"
    assert ws.list_files() == ["pkg/mod.py"]
    ws.delete("pkg/mod.py")
    assert not ws.exists("pkg/mod.py")
    ws.delete("pkg/mod.py")  # deleting again is safe


def test_local_workspace_missing_read_raises(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    with pytest.raises(WorkspaceError):
        ws.read_text("nope.py")


# --- CommandResult ------------------------------------------------------


def test_command_result_ok_and_output():
    ok = CommandResult(["x"], 0, "out", "")
    assert ok.ok is True
    assert ok.output == "out"
    bad = CommandResult(["x"], 1, "o", "e")
    assert bad.ok is False
    assert bad.output == "o\ne"


# --- SubprocessCommandRunner --------------------------------------------


def test_subprocess_success():
    runner = SubprocessCommandRunner()
    result = runner.run([sys.executable, "-c", "print('hi')"])
    assert result.ok
    assert "hi" in result.stdout


def test_subprocess_nonzero_exit():
    runner = SubprocessCommandRunner()
    result = runner.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.exit_code == 3


def test_subprocess_command_not_found():
    runner = SubprocessCommandRunner()
    result = runner.run(["this-command-does-not-exist-zzz"])
    assert result.exit_code == EXIT_NOT_FOUND
    assert not result.ok


def test_subprocess_timeout(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=0.1)

    monkeypatch.setattr(subprocess, "run", boom)
    runner = SubprocessCommandRunner(timeout=0.1)
    result = runner.run(["sleep", "5"])
    assert result.exit_code == EXIT_TIMEOUT
    assert "timed out" in result.stderr


# --- FakeCommandRunner --------------------------------------------------


def test_fake_runner_rules_and_default():
    runner = FakeCommandRunner().add_rule(
        "pytest", CommandResult(["pytest"], 0, "ok", "")
    )
    assert isinstance(runner, object)
    hit = runner.run(["pytest", "-q"])
    assert hit.ok and hit.stdout == "ok"
    miss = runner.run(["echo", "hi"])
    assert miss.exit_code == 0
    assert runner.calls == [["pytest", "-q"], ["echo", "hi"]]


def test_fake_runner_custom_default_exit():
    runner = FakeCommandRunner(default_exit_code=1)
    assert runner.run(["anything"]).exit_code == 1
