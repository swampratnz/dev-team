"""Tests for the engineer tool-loop workspace-containment hook."""

from __future__ import annotations

from helpers import run

from dev_team import agent_sandbox
from dev_team.agent_sandbox import GUARDED_TOOLS, workspace_containment_hook

CONTEXT = {"signal": None}


def _hook(root):
    matcher = workspace_containment_hook(root)
    assert matcher.matcher == GUARDED_TOOLS
    assert len(matcher.hooks) == 1
    return matcher.hooks[0]


def _call(hook, tool_name, tool_input, *, tool_use_id="t1"):
    return run(hook({"tool_name": tool_name, "tool_input": tool_input}, tool_use_id, CONTEXT))


def _is_denied(output) -> bool:
    return output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


# --- Read/Write/Edit: structured file_path ------------------------------


def test_in_root_file_path_is_allowed(tmp_path):
    hook = _hook(str(tmp_path))
    target = tmp_path / "src" / "x.py"
    target.parent.mkdir()
    target.write_text("x = 1")
    for tool in ("Read", "Write", "Edit"):
        result = _call(hook, tool, {"file_path": str(target)})
        assert result == {}


def test_relative_dotdot_escape_is_denied(tmp_path):
    hook = _hook(str(tmp_path / "root"))
    (tmp_path / "root").mkdir()
    result = _call(hook, "Read", {"file_path": "../escape.txt"})
    assert _is_denied(result)


def test_absolute_path_outside_root_is_denied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    hook = _hook(str(root))
    result = _call(hook, "Write", {"file_path": str(outside)})
    assert _is_denied(result)


def test_absolute_path_inside_root_is_allowed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Edit", {"file_path": str(root / "f.py")})
    assert result == {}


# --- SECURITY: symlink escape --------------------------------------------


def test_symlink_escape_is_denied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret outside data")
    (root / "escape.txt").symlink_to(outside)
    hook = _hook(str(root))
    result = _call(hook, "Read", {"file_path": str(root / "escape.txt")})
    assert _is_denied(result)


# --- fail-closed on malformed/missing paths ------------------------------


def test_missing_file_path_is_denied(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Read", {})
    assert _is_denied(result)


def test_non_string_file_path_is_denied(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Write", {"file_path": 123})
    assert _is_denied(result)


def test_empty_file_path_is_denied(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Edit", {"file_path": ""})
    assert _is_denied(result)


def test_malformed_tool_input_is_denied(tmp_path):
    hook = _hook(str(tmp_path))
    output = run(hook({"tool_name": "Read", "tool_input": "not-a-dict"}, "t1", CONTEXT))
    assert _is_denied(output)


def test_non_dict_input_data_is_denied(tmp_path):
    hook = _hook(str(tmp_path))
    output = run(hook("not-a-dict", "t1", CONTEXT))
    assert _is_denied(output)


def test_resolution_failure_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    # Build the hook (which resolves the root's real path) before patching, so
    # only the *candidate*'s resolution below is made to fail.
    hook = _hook(str(root))
    boom_target = str(root / "boom.txt")
    real_realpath = agent_sandbox.os.path.realpath

    def flaky(path):
        if path == boom_target:
            raise OSError("simulated resolution failure")
        return real_realpath(path)

    monkeypatch.setattr(agent_sandbox.os.path, "realpath", flaky)
    result = _call(hook, "Read", {"file_path": boom_target})
    assert _is_denied(result)


# --- Glob: optional path key ----------------------------------------------


def test_glob_without_path_defaults_to_root_and_is_allowed(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Glob", {"pattern": "**/*.py"})
    assert result == {}


def test_glob_with_in_root_path_is_allowed(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Glob", {"pattern": "*.py", "path": str(tmp_path)})
    assert result == {}


def test_glob_with_out_of_root_path_is_denied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Glob", {"pattern": "*.py", "path": str(tmp_path / "sibling")})
    assert _is_denied(result)


# --- Grep: optional path key ----------------------------------------------


def test_grep_without_path_defaults_to_root_and_is_allowed(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Grep", {"pattern": "TODO"})
    assert result == {}


def test_grep_with_in_root_path_is_allowed(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Grep", {"pattern": "TODO", "path": str(tmp_path)})
    assert result == {}


def test_grep_with_out_of_root_path_is_denied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Grep", {"pattern": "TODO", "path": str(tmp_path / "sibling")})
    assert _is_denied(result)


# --- Bash: heuristic string scan -----------------------------------------


def test_ordinary_bash_command_is_allowed(tmp_path):
    hook = _hook(str(tmp_path))
    # A leading space produces an empty split token, exercising the "falsy
    # token" filter branch distinctly from a token that just isn't escape-like.
    for command in ("pytest -q", "git status", "cat tests/test_x.py", " git status"):
        result = _call(hook, "Bash", {"command": command})
        assert result == {}


def test_bash_cd_dotdot_is_denied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Bash", {"command": "cd ../sibling && ls"})
    assert _is_denied(result)


def test_bash_absolute_out_of_root_token_is_denied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Bash", {"command": f"cat {tmp_path / 'outside.txt'}"})
    assert _is_denied(result)


def test_bash_absolute_in_root_token_is_allowed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Bash", {"command": f"cat {root / 'f.py'}"})
    assert result == {}


def test_bash_glued_redirection_dotdot_is_denied(tmp_path):
    # No space before ">" — a plain shell idiom, not adversarial obfuscation.
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Bash", {"command": "echo secret>../out.txt"})
    assert _is_denied(result)


def test_bash_glued_input_redirection_dotdot_is_denied(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hook = _hook(str(root))
    result = _call(hook, "Bash", {"command": "cat<../secret.txt"})
    assert _is_denied(result)


def test_bash_missing_command_is_denied(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Bash", {})
    assert _is_denied(result)


def test_bash_non_string_command_is_denied(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "Bash", {"command": ["not", "a", "string"]})
    assert _is_denied(result)


# --- unrelated tools pass through untouched -------------------------------


def test_unrelated_tool_is_allowed(tmp_path):
    hook = _hook(str(tmp_path))
    result = _call(hook, "WebFetch", {"url": "https://example.com"})
    assert result == {}


# --- deny reasons never leak an absolute path ------------------------------


def test_deny_reason_never_contains_an_absolute_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    hook = _hook(str(root))
    result = _call(hook, "Read", {"file_path": str(outside)})
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert str(outside) not in reason
    assert str(tmp_path) not in reason
