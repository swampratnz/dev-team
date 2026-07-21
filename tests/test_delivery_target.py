"""Tests for the delivery target (push the branch, open the PR).

The push goes through a :class:`FakeCommandRunner`-backed :class:`GitRepo` and
the PR through a :class:`FakePullRequestPublisher`, so nothing here touches the
network or a real repository. The security-critical property — the credential
never surviving into a raised error — is exercised end-to-end.
"""

from __future__ import annotations

import pytest

from dev_team.delivery_target import DeliveryTargetError, publish_pull_request
from dev_team.engine import DeliveryOutcome
from dev_team.execution import CommandResult, FakeCommandRunner
from dev_team.git import GitError, GitRepo
from dev_team.models import Design, FeatureRequest
from dev_team.pullrequest import FakePullRequestPublisher, PullRequest
from dev_team.report import render_delivery_summary
from dev_team.sources import RepoRef, git_auth_env


def _ref():
    return RepoRef(owner="acme", name="mono", url="https://github.com/acme/mono.git")


def _outcome(**kwargs):
    defaults = dict(
        request=FeatureRequest(title="Add /health", description="d"),
        plan_summary="p",
        design=Design(overview="o"),
        task_results=[],
        committed=True,
        branch="dev-team/health",
    )
    defaults.update(kwargs)
    return DeliveryOutcome(**defaults)


def test_push_branch_force_with_lease_carries_scrubbed_auth():
    from dev_team.delivery_target import push_branch

    cmd = FakeCommandRunner()
    push_branch("dev-team/health", ref=_ref(), token="TOK", git=GitRepo(cmd), force_with_lease=True)
    assert cmd.calls[-1] == [
        "git", "push", "--force-with-lease", "origin", "dev-team/health"
    ]
    assert "TOK" not in " ".join(cmd.calls[-1])
    assert "AUTHORIZATION: basic " in cmd.envs[-1]["GIT_CONFIG_VALUE_0"]


def test_push_branch_requires_a_token():
    from dev_team.delivery_target import push_branch

    with pytest.raises(DeliveryTargetError):
        push_branch("dev-team/health", ref=_ref(), token="", git=GitRepo(FakeCommandRunner()))


def test_publish_pushes_the_branch_and_opens_a_pr():
    ref = _ref()
    outcome = _outcome()
    cmd = FakeCommandRunner()
    publisher = FakePullRequestPublisher(result=PullRequest(7, "https://github.com/acme/mono/pull/7"))

    pr = publish_pull_request(
        outcome, ref=ref, token="TOK", git=GitRepo(cmd), publisher=publisher
    )

    # The branch was pushed with upstream tracking...
    assert cmd.calls[-1] == ["git", "push", "--set-upstream", "origin", "dev-team/health"]
    # ...carrying the credential only in the http.extraheader env, never argv.
    assert "TOK" not in " ".join(cmd.calls[-1])
    assert "AUTHORIZATION: basic " in cmd.envs[-1]["GIT_CONFIG_VALUE_0"]
    # The PR mirrors the outcome: title = feature, body = the run's summary.
    req = publisher.requests[-1]
    assert (req.owner, req.name, req.head, req.base, req.draft) == (
        "acme", "mono", "dev-team/health", "main", False
    )
    assert req.title == "Add /health"
    assert req.body == render_delivery_summary(outcome)
    assert pr == PullRequest(7, "https://github.com/acme/mono/pull/7")


def test_publish_honours_base_draft_remote_and_force_with_lease():
    ref = _ref()
    cmd = FakeCommandRunner()
    publisher = FakePullRequestPublisher()

    publish_pull_request(
        _outcome(),
        ref=ref,
        token="TOK",
        git=GitRepo(cmd),
        publisher=publisher,
        base="develop",
        draft=True,
        remote="upstream",
        force_with_lease=True,
    )

    assert cmd.calls[-1] == [
        "git", "push", "--set-upstream", "--force-with-lease", "upstream", "dev-team/health"
    ]
    req = publisher.requests[-1]
    assert req.base == "develop" and req.draft is True


def test_publish_refuses_an_uncommitted_delivery():
    with pytest.raises(DeliveryTargetError, match="nothing to publish"):
        publish_pull_request(
            _outcome(committed=False),
            ref=_ref(),
            token="TOK",
            git=GitRepo(FakeCommandRunner()),
            publisher=FakePullRequestPublisher(),
        )


def test_publish_refuses_when_no_branch_was_produced():
    # committed=True but branch is None (e.g. use_branch was off): still nothing
    # to open a PR against.
    with pytest.raises(DeliveryTargetError, match="nothing to publish"):
        publish_pull_request(
            _outcome(branch=None),
            ref=_ref(),
            token="TOK",
            git=GitRepo(FakeCommandRunner()),
            publisher=FakePullRequestPublisher(),
        )


def test_publish_requires_a_token():
    with pytest.raises(DeliveryTargetError, match="token is required"):
        publish_pull_request(
            _outcome(),
            ref=_ref(),
            token="",
            git=GitRepo(FakeCommandRunner()),
            publisher=FakePullRequestPublisher(),
        )


def test_push_branch_warns_before_pushing_a_ci_workflow_file():
    from dev_team.delivery_target import push_branch

    cmd = FakeCommandRunner()
    warnings = []
    push_branch(
        "dev-team/health",
        ref=_ref(),
        token="TOK",
        git=GitRepo(cmd),
        workspace_files=["src/x.py", ".github/workflows/ci.yml"],
        warn=warnings.append,
    )
    assert len(warnings) == 1
    assert ".github/workflows/ci.yml" in warnings[0]
    assert "workflow" in warnings[0]
    # The push still went through — this is advisory, not a block.
    assert cmd.calls[-1] == ["git", "push", "origin", "dev-team/health"]


def test_push_branch_default_warn_prints_to_stderr(capsys):
    from dev_team.delivery_target import push_branch

    push_branch(
        "dev-team/health",
        ref=_ref(),
        token="TOK",
        git=GitRepo(FakeCommandRunner()),
        workspace_files=[".github/workflows/ci.yml"],
    )
    err = capsys.readouterr().err
    assert ".github/workflows/ci.yml" in err
    assert "workflow" in err


def test_push_branch_does_not_warn_without_a_workflow_file():
    from dev_team.delivery_target import push_branch

    cmd = FakeCommandRunner()
    warnings = []
    push_branch(
        "dev-team/health",
        ref=_ref(),
        token="TOK",
        git=GitRepo(cmd),
        workspace_files=["src/x.py", "Dockerfile"],
        warn=warnings.append,
    )
    assert warnings == []


def test_publish_warns_before_pushing_a_workflow_file_in_the_outcome():
    ref = _ref()
    outcome = _outcome(workspace_files=["src/x.py", ".github/workflows/ci.yml"])
    warnings = []

    publish_pull_request(
        outcome,
        ref=ref,
        token="TOK",
        git=GitRepo(FakeCommandRunner()),
        publisher=FakePullRequestPublisher(),
        warn=warnings.append,
    )

    assert len(warnings) == 1
    assert ".github/workflows/ci.yml" in warnings[0]


def test_publish_scrubs_the_auth_header_from_a_push_failure():
    # The push carries the AUTHORIZATION: basic <base64> header in env; a
    # verbose/GIT_TRACE git can echo it back into its output. The scrub baked
    # into the target must strip that secret before it reaches the GitError —
    # the caller never has to remember to opt in.
    ref = _ref()
    token = "ghp_supersecretvalue"
    header = git_auth_env(ref, token)["GIT_CONFIG_VALUE_0"]
    basic_b64 = header.split("basic ", 1)[1]
    cmd = FakeCommandRunner().add_rule(
        "push",
        CommandResult(["git", "push"], 128, "", f"fatal: remote rejected; sent {header}"),
    )
    with pytest.raises(GitError) as exc:
        publish_pull_request(
            _outcome(), ref=ref, token=token, git=GitRepo(cmd),
            publisher=FakePullRequestPublisher(),
        )
    msg = str(exc.value)
    assert basic_b64 not in msg and token not in msg
    assert "***" in msg
