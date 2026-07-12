"""Tests for the repository source layer (--repo clone + PAT handling)."""

from __future__ import annotations

import base64
import os

import pytest

from dev_team.execution import (
    CommandResult,
    FakeCommandRunner,
    SubprocessCommandRunner,
)
from dev_team.git import GitRepo
from dev_team.sources import (
    RepoRef,
    SourceError,
    clone_or_update,
    load_env_file,
    parse_repo,
    resolve_github_token,
)


# --- parsing ---------------------------------------------------------------------


def test_parse_repo_slug_resolves_to_github():
    ref = parse_repo("acme/legacy-monolith")
    assert ref.owner == "acme"
    assert ref.name == "legacy-monolith"
    assert ref.url == "https://github.com/acme/legacy-monolith.git"
    assert ref.slug == "acme/legacy-monolith"
    assert ref.workspace_name == "acme__legacy-monolith"


def test_parse_repo_slug_strips_git_suffix():
    assert parse_repo("acme/thing.git").url == "https://github.com/acme/thing.git"
    assert parse_repo("acme/thing.git").name == "thing"


def test_parse_repo_https_url_passes_through():
    ref = parse_repo("https://github.com/acme/thing.git")
    assert ref.url == "https://github.com/acme/thing.git"
    assert (ref.owner, ref.name) == ("acme", "thing")
    ref = parse_repo("https://gitlab.example.com/group/sub/thing")
    assert (ref.owner, ref.name) == ("sub", "thing")


def test_parse_repo_ssh_url():
    ref = parse_repo("git@github.com:acme/thing.git")
    assert ref.url == "git@github.com:acme/thing.git"
    assert (ref.owner, ref.name) == ("acme", "thing")


def test_parse_repo_rejects_junk():
    for bad in ("not a repo", "https://github.com", "git@nohost", "owner/name/extra"):
        with pytest.raises(SourceError):
            parse_repo(bad)


# --- env file & token resolution ---------------------------------------------------


def test_load_env_file_parses_key_values(tmp_path):
    env = tmp_path / "prod.env"
    env.write_text(
        "# credentials\n"
        "\n"
        "GITHUB_TOKEN=github_pat_abc123\n"
        'QUOTED="with spaces"\n'
        "export EXPORTED='single'\n"
        "no_equals_line\n"
        "=orphaned value\n"
    )
    values = load_env_file(str(env))
    assert values == {
        "GITHUB_TOKEN": "github_pat_abc123",
        "QUOTED": "with spaces",
        "EXPORTED": "single",
    }


def test_load_env_file_missing_is_an_error(tmp_path):
    with pytest.raises(SourceError, match="env file not found"):
        load_env_file(str(tmp_path / "nope.env"))


def test_resolve_token_prefers_env_file_and_scrubs_process_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GITHUB_TOKEN=from-file\n")
    environ = {"GITHUB_TOKEN": "from-process", "GH_TOKEN": "also-process", "PATH": "/x"}
    token = resolve_github_token(str(env), environ=environ)
    assert token == "from-file"
    # both process copies are gone even though the file supplied the token
    assert environ == {"PATH": "/x"}


def test_resolve_token_falls_back_to_process_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("UNRELATED=1\n")
    environ = {"GH_TOKEN": "inherited"}
    assert resolve_github_token(str(env), environ=environ) == "inherited"
    assert environ == {}


def test_resolve_token_orders_keys_and_handles_absence():
    environ = {"GITHUB_TOKEN": "first", "GH_TOKEN": "second"}
    assert resolve_github_token(environ=environ) == "first"
    assert environ == {}
    assert resolve_github_token(environ={}) is None


def test_resolve_token_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "live-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert resolve_github_token() == "live-token"
    assert "GITHUB_TOKEN" not in os.environ


# --- clone & update ----------------------------------------------------------------


_REF = RepoRef(owner="acme", name="thing", url="https://github.com/acme/thing.git")


def _expected_header(token):
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {basic}"


def test_clone_authenticates_via_env_never_argv(tmp_path):
    runner = FakeCommandRunner()
    dest = str(tmp_path / "clone")
    result = clone_or_update(_REF, dest, runner=runner, token="github_pat_secret")
    assert result == dest
    assert runner.calls == [["git", "clone", _REF.url, dest]]
    env = runner.envs[0]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == _expected_header("github_pat_secret")
    # nothing token-shaped in the command line
    assert "github_pat_secret" not in " ".join(runner.calls[0])


def test_clone_without_token_or_on_non_https_sends_no_header(tmp_path):
    runner = FakeCommandRunner()
    clone_or_update(_REF, str(tmp_path / "a"), runner=runner, token=None)
    assert "GIT_CONFIG_KEY_0" not in runner.envs[0]
    file_ref = RepoRef(owner="o", name="n", url="file:///srv/mirror/n.git")
    clone_or_update(file_ref, str(tmp_path / "b"), runner=runner, token="tok")
    assert "GIT_CONFIG_KEY_0" not in runner.envs[1]
    assert runner.envs[1]["GIT_TERMINAL_PROMPT"] == "0"


def test_clone_into_empty_existing_directory_is_fine(tmp_path):
    dest = tmp_path / "empty"
    dest.mkdir()
    runner = FakeCommandRunner()
    clone_or_update(_REF, str(dest), runner=runner)
    assert runner.calls[0][:2] == ["git", "clone"]


def test_clone_refuses_a_non_git_occupied_destination(tmp_path):
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "file.txt").write_text("x")
    with pytest.raises(SourceError, match="not a git repository"):
        clone_or_update(_REF, str(dest), runner=FakeCommandRunner())


def test_clone_failure_scrubs_token_and_hints_on_404(tmp_path):
    runner = FakeCommandRunner()
    runner.add_rule(
        "git clone",
        CommandResult(
            ["git", "clone"], 128, "", "fatal: repository not found (tok-123 leaked)"
        ),
    )
    with pytest.raises(SourceError) as excinfo:
        clone_or_update(_REF, str(tmp_path / "c"), runner=runner, token="tok-123")
    message = str(excinfo.value)
    assert "404" in message or "cannot read" in message  # the private-repo hint
    assert "tok-123" not in message
    assert "***" in message


def test_clone_failure_without_404_has_no_hint(tmp_path):
    runner = FakeCommandRunner()
    runner.add_rule(
        "git clone", CommandResult(["git", "clone"], 128, "", "network unreachable")
    )
    with pytest.raises(SourceError) as excinfo:
        clone_or_update(_REF, str(tmp_path / "c"), runner=runner)
    assert "cannot read" not in str(excinfo.value)
    assert "network unreachable" in str(excinfo.value)


def _existing_clone(tmp_path, remote_url):
    dest = tmp_path / "existing"
    (dest / ".git").mkdir(parents=True)
    runner = FakeCommandRunner()
    runner.add_rule(
        "remote get-url", CommandResult(["git"], 0, remote_url + "\n", "")
    )
    return str(dest), runner


def test_existing_clone_of_same_remote_is_fast_forwarded(tmp_path):
    dest, runner = _existing_clone(tmp_path, _REF.url)
    assert clone_or_update(_REF, dest, runner=runner, token="tok") == dest
    assert runner.calls[1][:3] == ["git", "pull", "--ff-only"]
    assert runner.envs[1]["GIT_CONFIG_VALUE_0"] == _expected_header("tok")


def test_existing_clone_of_other_remote_is_refused(tmp_path):
    dest, runner = _existing_clone(tmp_path, "https://github.com/else/where.git")
    with pytest.raises(SourceError, match="different --workspace"):
        clone_or_update(_REF, dest, runner=runner)


def test_existing_clone_update_failure_is_loud(tmp_path):
    dest, runner = _existing_clone(tmp_path, _REF.url)
    runner.add_rule(
        "pull --ff-only",
        CommandResult(["git"], 1, "", "fatal: Not possible to fast-forward"),
    )
    with pytest.raises(SourceError, match="could not update"):
        clone_or_update(_REF, dest, runner=runner)


# --- against the real git binary ---------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def test_clone_and_update_a_real_repository(tmp_path):
    source = tmp_path / "origin"
    source.mkdir()
    upstream = GitRepo(SubprocessCommandRunner(), cwd=str(source))
    upstream.ensure_repo()
    (source / "README.md").write_text("# hello\n")
    upstream.add_paths(["README.md"])
    upstream.commit("initial")

    ref = parse_repo(f"file://{source}")
    runner = SubprocessCommandRunner()
    dest = str(tmp_path / "work" / ref.workspace_name)
    assert clone_or_update(ref, dest, runner=runner) == dest
    assert (tmp_path / "work" / ref.workspace_name / "README.md").read_text() == "# hello\n"

    (source / "NEW.txt").write_text("more\n")
    upstream.add_paths(["NEW.txt"])
    upstream.commit("more")
    clone_or_update(ref, dest, runner=runner)  # fast-forward, not re-clone
    assert (tmp_path / "work" / ref.workspace_name / "NEW.txt").exists()


def test_clone_failure_hints_when_no_credential_was_usable(tmp_path):
    runner = FakeCommandRunner()
    runner.add_rule(
        "git clone",
        CommandResult(
            ["git", "clone"], 128, "",
            "fatal: could not read Username for 'https://github.com': "
            "terminal prompts disabled",
        ),
    )
    with pytest.raises(SourceError, match="no usable credential"):
        clone_or_update(_REF, str(tmp_path / "c"), runner=runner)
