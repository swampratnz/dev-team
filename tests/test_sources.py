"""Tests for the repository source layer (--repo clone + PAT handling)."""

from __future__ import annotations

import base64
import os
from pathlib import Path

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
    is_github_repo,
    load_env_file,
    parse_repo,
    resolve_github_token,
)


def test_is_github_repo_across_url_forms():
    for on_github in (
        "acme/legacy",  # bare slug → https://github.com
        "https://github.com/acme/legacy.git",
        "https://www.github.com/acme/legacy",
        "ssh://git@github.com/acme/legacy",
        "git@github.com:acme/legacy.git",
    ):
        assert is_github_repo(parse_repo(on_github)), on_github
    for off_github in (
        "https://internal-git.corp.example/acme/legacy",
        "ssh://git@internal.example/acme/x",
        "git@evil.example:acme/x.git",
        "git://10.0.0.1/acme/x",
        "https://api.github.com/acme/x",  # REST host, not a clone remote
    ):
        assert not is_github_repo(parse_repo(off_github)), off_github
    # A hostless ref (a bare local path, or a colon whose authority is a
    # path) has no host to trust — never github.
    for hostless in (
        RepoRef(owner="a", name="b", url="/tmp/local/repo"),
        RepoRef(owner="a", name="b", url="./weird/path:branch"),
    ):
        assert not is_github_repo(hostless), hostless.url


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


def test_parse_repo_rejects_dot_segment_owner_or_name():
    # A crafted URL must not derive an owner/name of "." or ".." — reachable
    # over the dispatch API (job submit, GET /checks), so it is constrained
    # like the bare-slug form. (authorises_repo would 403 such an owner
    # anyway, but the parse fails closed rather than relying on that.)
    for bad in (
        "https://github.com/../evil.git",
        "https://github.com/../../evil",
        "git@github.com:../evil.git",
        "https://github.com/owner/..",
    ):
        with pytest.raises(SourceError):
            parse_repo(bad)


def test_parse_repo_rejects_url_hostile_owner_or_name():
    # URL/scp forms derive owner/name from raw path segments and feed them
    # unescaped into GitHub API paths and the App token-mint body, so a
    # crafted repo must not smuggle URL-significant or control characters
    # (percent-encoded or literal) into either segment.
    for bad in (
        "https://github.com/acme/name%3Fx",   # %-encoded '?'
        "https://github.com/acme/name%23y",   # %-encoded '#'
        "https://github.com/ac%20me/name",    # %-encoded space in owner
        "git@github.com:acme/na me.git",      # literal space
        "https://github.com/acme/name#frag",  # a real fragment on the name
    ):
        with pytest.raises(SourceError):
            parse_repo(bad)
    # legitimate owner/name characters (alnum, '-', '_', '.') still parse
    ref = parse_repo("https://github.com/acme-org/my_repo.v2.git")
    assert (ref.owner, ref.name) == ("acme-org", "my_repo.v2")


def test_parse_repo_rejects_embedded_credentials():
    # A URL carrying credentials would leak them into argv and .git/config,
    # out of reach of the header-auth design and the scrubber; refuse it.
    for bad in (
        "https://user:pass@github.com/acme/thing.git",
        "https://ghp_token@github.com/acme/thing.git",
    ):
        with pytest.raises(SourceError, match="embedded credentials"):
            parse_repo(bad)


def test_parse_repo_allows_ssh_scheme_transport_user():
    # A bare ssh:// transport username (no password) is not a secret.
    ref = parse_repo("ssh://git@github.com/acme/thing.git")
    assert (ref.owner, ref.name) == ("acme", "thing")
    assert ref.url == "ssh://git@github.com/acme/thing.git"


def test_parse_repo_rejects_file_scheme_by_default():
    # SECURITY (S3): file:// is local file access, reachable from the
    # authenticated dispatch API — refused unless a trusted caller opts in.
    with pytest.raises(SourceError, match="unsupported URL scheme 'file'"):
        parse_repo("file:///etc")


def test_parse_repo_allows_file_scheme_only_with_allow_local():
    ref = parse_repo("file:///srv/mirror/thing.git", allow_local=True)
    assert ref.url == "file:///srv/mirror/thing.git"
    assert (ref.owner, ref.name) == ("mirror", "thing")


def test_parse_repo_rejects_rce_capable_ext_transport():
    # SECURITY (S3): git's ext:: helper transport runs an arbitrary command;
    # it is never a legitimate repo reference and must be refused (even with
    # allow_local, which only re-admits file://).
    for allow_local in (False, True):
        with pytest.raises(SourceError):
            parse_repo("ext::sh -c id", allow_local=allow_local)


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


def test_clone_non_github_https_sends_no_header(tmp_path):
    # A GitHub token must never be attached to some other host just because the
    # transport is https — that would hand the credential to a third party.
    runner = FakeCommandRunner()
    other = RepoRef(
        owner="group", name="thing", url="https://gitlab.example.com/group/thing.git"
    )
    clone_or_update(other, str(tmp_path / "g"), runner=runner, token="tok")
    assert "GIT_CONFIG_KEY_0" not in runner.envs[0]
    assert runner.envs[0]["GIT_TERMINAL_PROMPT"] == "0"


def test_clone_attaches_header_for_github_api_and_www_hosts(tmp_path):
    runner = FakeCommandRunner()
    for i, host in enumerate(("api.github.com", "www.github.com", "GitHub.com")):
        ref = RepoRef(owner="o", name="n", url=f"https://{host}/o/n.git")
        clone_or_update(ref, str(tmp_path / f"h{i}"), runner=runner, token="tok")
        assert runner.envs[i]["GIT_CONFIG_VALUE_0"] == _expected_header("tok")


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


def test_clone_failure_scrubs_the_basic_auth_header_too(tmp_path):
    # A verbose/GIT_TRACE line can echo the computed AUTHORIZATION header, not
    # just the raw token; the base64 value must be redacted as well.
    token = "tok-xyz"
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    runner = FakeCommandRunner()
    runner.add_rule(
        "git clone",
        CommandResult(
            ["git", "clone"], 128, "", f"trace: AUTHORIZATION: basic {basic}"
        ),
    )
    with pytest.raises(SourceError) as excinfo:
        clone_or_update(_REF, str(tmp_path / "c"), runner=runner, token=token)
    message = str(excinfo.value)
    assert basic not in message
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

    # allow_local=True re-admits the file:// transport for a trusted local
    # caller (the default authenticated path rejects it — see the scheme tests).
    ref = parse_repo(
        f"file://{source.as_posix()}", allow_local=True
    )  # forward slashes on every OS
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


# --- default env-file discovery -----------------------------------------------------


def test_default_env_file_prefers_cwd_then_user_config(tmp_path, monkeypatch):
    from dev_team.sources import default_env_file

    monkeypatch.chdir(tmp_path)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert default_env_file() is None  # nothing configured anywhere

    user_file = xdg / "dev-team" / "dev-team.env"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("GITHUB_TOKEN=user-level\n")
    assert default_env_file() == str(user_file)

    (tmp_path / ".env").write_text("GITHUB_TOKEN=project-level\n")
    assert default_env_file() == str(Path(".env"))  # cwd wins over user config


def test_default_env_file_explicit_candidates_cover_system_path(tmp_path):
    from dev_team.sources import default_env_file

    system = tmp_path / "etc" / "dev-team" / "dev-team.env"
    system.parent.mkdir(parents=True)
    system.write_text("GITHUB_TOKEN=system-level\n")
    found = default_env_file(candidates=(tmp_path / "missing.env", system))
    assert found == str(system)


def test_default_env_file_xdg_default_is_home_config(monkeypatch, tmp_path):
    from dev_team.sources import default_env_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows spells HOME this way
    target = tmp_path / ".config" / "dev-team" / "dev-team.env"
    target.parent.mkdir(parents=True)
    target.write_text("GITHUB_TOKEN=home-config\n")
    assert default_env_file() == str(target)
