"""Publish a completed delivery as a pull request (the delivery terminus).

ROADMAP #2: a dev team's real interface is a pull request reviewed by humans and
CI, not a local commit. This composes the two primitives from the previous step
— :meth:`dev_team.git.GitRepo.push` and a
:class:`dev_team.pullrequest.PullRequestPublisher` — into the one action a
caller runs after a successful, committed delivery: push the delivery branch to
the remote, then open a PR whose body is the run's own outcome report.

Token hygiene is *built in here*, never left to the caller to remember: the
credential rides only in the per-command ``GIT_CONFIG_*`` ``http.extraheader``
env (:func:`dev_team.sources.git_auth_env`) — never argv/URL/``.git/config`` —
and git's own output is scrubbed of it (:func:`dev_team.sources.scrub_credentials`)
before any error is raised, exactly as :func:`dev_team.sources.clone_or_update`
pairs the two for the clone path. A caller cannot opt out of the redaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import DevTeamError
from .git import GitRepo
from .pullrequest import PullRequest, PullRequestPublisher, PullRequestRequest
from .report import render_delivery_summary
from .sources import RepoRef, git_auth_env, scrub_credentials

if TYPE_CHECKING:  # pragma: no cover - types only; avoids importing the engine
    from .engine import DeliveryOutcome


class DeliveryTargetError(DevTeamError):
    """The delivery could not be published as a pull request."""


def push_branch(
    branch: str,
    *,
    ref: RepoRef,
    token: str,
    git: GitRepo,
    remote: str = "origin",
    set_upstream: bool = False,
    force_with_lease: bool = False,
) -> None:
    """Push ``branch`` to ``remote`` with token hygiene baked in.

    The credential rides only in the per-command ``http.extraheader`` env and is
    scrubbed from any error — never left to the caller to remember. Used both
    for the initial publish push and for re-pushing a CI fix to an open PR's
    branch (``force_with_lease``, never a bare ``--force``).
    """

    if not token:
        raise DeliveryTargetError(
            "a GitHub token is required to push the branch; none was resolved "
            "(set --env-file or GITHUB_TOKEN)"
        )
    git.push(
        branch,
        remote=remote,
        set_upstream=set_upstream,
        force_with_lease=force_with_lease,
        env=git_auth_env(ref, token),
        scrub=lambda text: scrub_credentials(text, token),
    )


def publish_pull_request(
    outcome: "DeliveryOutcome",
    *,
    ref: RepoRef,
    token: str,
    git: GitRepo,
    publisher: PullRequestPublisher,
    base: str = "main",
    draft: bool = False,
    remote: str = "origin",
    force_with_lease: bool = False,
) -> PullRequest:
    """Push ``outcome``'s branch to ``remote`` and open a PR for it.

    Refuses when there is nothing to publish — the delivery must have committed
    work to a named branch (a PR over an empty or uncommitted delivery would be
    a no-op at best, a false record at worst) — and when no ``token`` is
    available to authenticate the push and the API call. The PR title is the
    feature title and its body is :func:`~dev_team.report.render_delivery_summary`
    of the run, so reviewers see exactly what the team did and how it fared.

    The push always carries the auth env and a scrub redactor, so a verbose /
    ``GIT_TRACE`` failure cannot leak the ``AUTHORIZATION: basic <base64>``
    header into the raised error.
    """

    if not outcome.committed or not outcome.branch:
        raise DeliveryTargetError(
            "nothing to publish: the delivery did not commit work to a branch, "
            "so there is no pull request to open (needs a committed delivery on "
            "a named branch)"
        )
    if not token:
        raise DeliveryTargetError(
            "a GitHub token is required to push the branch and open the pull "
            "request; none was resolved (set --env-file or GITHUB_TOKEN)"
        )
    push_branch(
        outcome.branch,
        ref=ref,
        token=token,
        git=git,
        remote=remote,
        set_upstream=True,
        force_with_lease=force_with_lease,
    )
    request = PullRequestRequest(
        owner=ref.owner,
        name=ref.name,
        title=outcome.request.title,
        body=render_delivery_summary(outcome),
        head=outcome.branch,
        base=base,
        draft=draft,
    )
    return publisher.open(request)
