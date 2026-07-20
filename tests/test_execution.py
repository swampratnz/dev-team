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


def test_normalise_rejects_windows_style_escape():
    # Splitting on backslash too means a Windows-style ``..\..\x`` decomposes
    # into ``..`` segments and is rejected, not passed through as one opaque
    # component that would escape the root.
    with pytest.raises(WorkspaceError):
        _normalise("..\\..\\x")


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


def test_in_memory_delete_dir_removes_only_the_prefixed_keys():
    ws = InMemoryWorkspace(
        {
            "dir/a.txt": "1",
            "dir/nested/b.txt": "2",
            "dir-sibling/x.txt": "keep",  # shared string prefix, different segment
            "outside.txt": "keep",
        }
    )
    ws.delete_dir("dir")
    assert ws.list_files() == ["dir-sibling/x.txt", "outside.txt"]


def test_in_memory_delete_dir_missing_prefix_is_a_noop():
    ws = InMemoryWorkspace({"a.txt": "1"})
    ws.delete_dir("nope")
    assert ws.list_files() == ["a.txt"]


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


def test_local_workspace_list_files_skips_excluded_dirs(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    ws.write_text("src/mod.py", "code")
    ws.write_text(".git/config", "junk")  # a tooling-internal dir, never listed
    assert ws.list_files() == ["src/mod.py"]


def test_local_workspace_delete_dir_removes_a_populated_nested_directory(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    ws.write_text("dir/a.txt", "1")
    ws.write_text("dir/nested/b.txt", "2")
    ws.write_text("outside.txt", "keep")
    ws.delete_dir("dir")
    assert not (tmp_path / "dir").exists()
    assert ws.list_files() == ["outside.txt"]


def test_local_workspace_delete_dir_missing_directory_is_a_noop(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    ws.delete_dir("nope")  # no exception


def test_local_workspace_delete_dir_on_a_file_path_is_a_noop(tmp_path):
    # delete_dir is directory-only, delete() stays file-only -- no overlap.
    ws = LocalWorkspace(str(tmp_path))
    ws.write_text("a.txt", "1")
    ws.delete_dir("a.txt")
    assert ws.exists("a.txt")


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


def test_subprocess_timeout_decodes_bytes_partial_output(monkeypatch):
    # TimeoutExpired.stdout arrives as bytes on some platforms even under
    # text=True; force that shape so the decode branch is covered everywhere.
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="x", timeout=0.1, output=b"partial bytes")

    monkeypatch.setattr(subprocess, "run", boom)
    result = SubprocessCommandRunner().run(["x"], timeout=0.1)
    assert result.exit_code == EXIT_TIMEOUT
    assert result.stdout == "partial bytes"


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


def test_dry_run_command_runner_is_honest():
    from dev_team.execution import DryRunCommandRunner

    runner = DryRunCommandRunner()
    result = runner.run(["pytest", "-q"])
    assert result.ok
    assert "not executed" in result.stdout
    assert runner.calls == [["pytest", "-q"]]


def test_subprocess_runner_timeout_keeps_partial_output_usable():
    import sys

    runner = SubprocessCommandRunner()
    result = runner.run(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"],
        timeout=1.0,
    )
    assert result.exit_code == EXIT_TIMEOUT
    assert "partial" in result.stdout
    # TimeoutExpired carries bytes; output must still be a string join
    assert "command timed out" in result.output


def test_local_workspace_write_is_atomic_and_leaves_no_staging_file(tmp_path):
    ws = LocalWorkspace(str(tmp_path))
    ws.write_text("data.json", '{"ok": true}')
    ws.write_text("data.json", '{"ok": false}')
    assert ws.read_text("data.json") == '{"ok": false}'
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".dev-team-tmp")]
    assert leftovers == []


def test_subprocess_env_overlays_inherited_environment():
    import os
    import sys

    from dev_team.execution import SubprocessCommandRunner

    runner = SubprocessCommandRunner()
    code = "import os; print(os.environ['DT_EXTRA'], 'PATH' in os.environ)"
    result = runner.run(
        [sys.executable, "-c", code], env={"DT_EXTRA": "overlay-value"}
    )
    assert result.ok
    assert result.stdout.strip() == "overlay-value True"
    assert "DT_EXTRA" not in os.environ  # the overlay never leaks back


def test_dry_run_and_fake_runner_accept_env():
    from dev_team.execution import DryRunCommandRunner, FakeCommandRunner

    dry = DryRunCommandRunner()
    assert dry.run(["x"], env={"A": "1"}).ok
    fake = FakeCommandRunner()
    fake.run(["x"], env={"A": "1"})
    fake.run(["y"])
    assert fake.envs == [{"A": "1"}, None]


# --- secret env scrubbing (A2) ------------------------------------------


def test_subprocess_scrubs_secret_env_keys_from_child(monkeypatch):
    from dev_team.execution import SECRET_ENV_KEYS, SubprocessCommandRunner

    secret = next(iter(SECRET_ENV_KEYS))
    monkeypatch.setenv(secret, "top-secret")
    runner = SubprocessCommandRunner()
    code = f"import os; print(os.environ.get({secret!r}, 'ABSENT'))"
    # env=None still runs against a *scrubbed* copy of the parent environment,
    # so agent-authored gate/test code cannot read the credential.
    result = runner.run([sys.executable, "-c", code])
    assert result.ok
    assert result.stdout.strip() == "ABSENT"


def test_subprocess_explicitly_passed_secret_is_visible(monkeypatch):
    from dev_team.execution import SECRET_ENV_KEYS, SubprocessCommandRunner

    secret = next(iter(SECRET_ENV_KEYS))
    monkeypatch.setenv(secret, "from-parent")
    runner = SubprocessCommandRunner()
    code = f"import os; print(os.environ.get({secret!r}, 'ABSENT'))"
    # An explicit per-command value (e.g. a scoped git auth header) still wins
    # over the scrub — the caller opted in deliberately.
    result = runner.run([sys.executable, "-c", code], env={secret: "explicit"})
    assert result.ok
    assert result.stdout.strip() == "explicit"


# --- symlink containment (A3) -------------------------------------------


def test_local_workspace_refuses_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret outside data")
    ws = LocalWorkspace(str(root))
    (root / "escape.txt").symlink_to(outside)
    # a symlink whose real target is outside the root is refused for reads ...
    with pytest.raises(WorkspaceError):
        ws.read_text("escape.txt")
    # ... and excluded from listing entirely
    assert ws.list_files() == []


def test_local_workspace_delete_dir_refuses_textual_escape(tmp_path):
    ws = LocalWorkspace(str(tmp_path / "root"))
    with pytest.raises(WorkspaceError):
        ws.delete_dir("../escape")


def test_local_workspace_delete_dir_refuses_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not delete me")
    ws = LocalWorkspace(str(root))
    (root / "escape_dir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        ws.delete_dir("escape_dir")
    assert outside.exists()
    assert (outside / "secret.txt").read_text() == "do not delete me"


# --- unique staging paths (A5) ------------------------------------------


def test_local_workspace_uses_unique_staging_paths(tmp_path, monkeypatch):
    import pathlib

    ws = LocalWorkspace(str(tmp_path))
    seen = []
    real_replace = pathlib.Path.replace

    def spy_replace(self, target):
        seen.append(str(self))  # ``self`` is the staging file being renamed in
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", spy_replace)
    ws.write_text("data.txt", "one")
    ws.write_text("data.txt", "two")
    assert len(seen) == 2
    # a fixed staging name would collide here; concurrent writers must not
    assert seen[0] != seen[1]
    assert ws.read_text("data.txt") == "two"


# --- capture truncation (A6) --------------------------------------------


def test_truncate_caps_output_and_marks_elision(monkeypatch):
    from dev_team import execution

    monkeypatch.setattr(execution, "MAX_CAPTURE_CHARS", 10)
    capped = execution._truncate("x" * 50)
    assert capped.startswith("x" * 10)
    assert "truncated 40 chars" in capped
    # short output is returned verbatim
    assert execution._truncate("short") == "short"


def test_subprocess_truncates_captured_stdout(monkeypatch):
    from dev_team import execution

    monkeypatch.setattr(execution, "MAX_CAPTURE_CHARS", 20)
    runner = execution.SubprocessCommandRunner()
    result = runner.run([sys.executable, "-c", "print('y' * 1000)"])
    assert result.ok
    assert "truncated" in result.stdout
    assert len(result.stdout) < 200
