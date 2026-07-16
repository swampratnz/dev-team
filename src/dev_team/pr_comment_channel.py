"""Supervise the CI-fix loop from the pull request itself (ROADMAP #7).

The interactive core (plan review, escalation, approvals) lives on the
UI-agnostic :class:`~dev_team.interaction.InteractionChannel`, but the only
question that fires *after* a PR is open is ``ci_fix_question`` (asked from
``_run_ci_fix_loop`` in ``cli.py``). This module is the "the pull request
itself" adapter ROADMAP item 7 names: it posts the question as a PR comment
and polls for an authorized, matching reply — no new credential, no LLM call,
reusing the exact injectable-HTTP / token-in-header-only shape of
:mod:`dev_team.pullrequest` and the bounded-poll shape of
:mod:`dev_team.checks`.

**Authorization is a closed allow-list, not "anyone who can comment"** — the
load-bearing security control. Only replies from a configured set of GitHub
logins are honoured; every other comment is silently skipped. There is no
implicit default (e.g. "the PR author") — the caller must supply the
allow-list explicitly, so an empty list is not a soft-fail special case: it
simply matches nothing and the channel degrades to its fail-safe once the
poll bound is exhausted, exactly like an unauthorized reply would.

Enabling this channel changes the **audience** of the posted content: today
``ci_fix_question``'s context (CI failure output, Restricted-classified) is
seen only on a private terminal or through dispatch's bearer-token-gated
endpoint. Posting it as a plain PR comment makes it world-readable on a
public repo — see ``docs/INTERACTION.md`` for the operator-facing warning.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .errors import DevTeamError
from .interaction import Question, Reply

#: Injectable transport for posting a comment: ``(url, body, headers) -> JSON``.
PostHttp = Callable[[str, bytes, Mapping[str, str]], Dict]
#: Injectable transport for listing comments: ``(url, headers) -> JSON list``.
GetHttp = Callable[[str, Mapping[str, str]], List[Dict]]

_API_BASE = "https://api.github.com"
_API_VERSION = "2022-11-28"
_HTTP_TIMEOUT_SECONDS = 30.0


class GitHubPRCommentChannelError(DevTeamError):
    """Posting or polling the PR's comments failed (auth, validation, transport)."""


def _http_post(url: str, body: bytes, headers: Mapping[str, str]) -> Dict:
    """POST ``body`` to ``url`` and return the parsed JSON response (default)."""

    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_get(url: str, headers: Mapping[str, str]) -> List[Dict]:
    """GET ``url`` and return the parsed JSON response (default)."""

    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _render_comment_body(question: Question) -> str:
    """The question rendered as a PR comment: prompt, context, reply menu."""

    lines = [f"**{question.asked_by} asks:** {question.prompt}"]
    if question.context:
        lines.append("")
        lines.append(question.context)
    lines.append("")
    menu = ", ".join(f"`{choice.key}`" for choice in question.choices)
    lines.append(f"Reply with one of: {menu}")
    return "\n".join(lines)


@dataclass
class GitHubPRCommentChannel:
    """An :class:`~dev_team.interaction.InteractionChannel` backed by PR comments.

    :meth:`ask` posts ``question`` as one issue comment on the PR, then polls
    (bounded by ``max_polls``/``poll_interval_seconds``) for a comment from an
    **authorized** login (``allowed_logins`` — case-insensitive, no default)
    whose first whitespace-trimmed, lower-cased token exactly matches one of
    the question's live choice keys. An unauthorized commenter or an
    unrecognised reply is silently skipped, not fuzzy-matched. Once the poll
    bound is exhausted with no authorized matching reply, ``ask`` returns the
    question's fail-safe choice — mirroring :class:`~dev_team.interaction.ConsoleChannel`'s
    EOF behaviour exactly, so an unanswered round never auto-applies an
    unreviewed change.
    """

    token: str = field(repr=False)  # never let a repr/traceback/config dump print it
    owner: str = ""
    name: str = ""
    pr_number: int = 0
    allowed_logins: Sequence[str] = ()
    api_base: str = _API_BASE
    max_polls: int = 30
    poll_interval_seconds: float = 20.0
    sleep: Callable[[float], None] = time.sleep
    http_post: Optional[PostHttp] = field(default=None, repr=False)  # a bound fake may capture secrets
    http_get: Optional[GetHttp] = field(default=None, repr=False)  # a bound fake may capture secrets

    def ask(self, question: Question) -> Reply:
        posted = self._post(question)
        posted_id = posted.get("id")
        since = str(posted.get("created_at") or "")
        allowed = {login.strip().lower() for login in self.allowed_logins if login.strip()}
        for attempt in range(self.max_polls):
            if attempt:
                self.sleep(self.poll_interval_seconds)
            for comment in self._poll(since):
                if comment.get("id") == posted_id:
                    continue  # our own posted question, not a reply
                login = str((comment.get("user") or {}).get("login") or "").strip().lower()
                if not login or login not in allowed:
                    continue
                body = str(comment.get("body") or "").strip()
                if not body:
                    continue
                choice = question.find(body.split()[0])
                if choice is not None:
                    return Reply(choice=choice.key)
        return Reply(choice=question.fail_safe.key)

    def _post(self, question: Question) -> Dict:
        url = f"{self.api_base}/repos/{self.owner}/{self.name}/issues/{self.pr_number}/comments"
        payload = {"body": _render_comment_body(question)}
        http = self.http_post or _http_post
        try:
            response = http(url, json.dumps(payload).encode("utf-8"), self._headers())
        except urllib.error.HTTPError as exc:
            raise GitHubPRCommentChannelError(
                self._describe(exc, "posting the CI-fix question")
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubPRCommentChannelError(
                self._scrub(f"could not reach {self.api_base}: {exc.reason}")
            ) from exc
        return response if isinstance(response, dict) else {}

    def _poll(self, since: str) -> List[Dict]:
        query = urllib.parse.urlencode({"since": since}) if since else ""
        url = (
            f"{self.api_base}/repos/{self.owner}/{self.name}/issues/{self.pr_number}/comments"
            f"{'?' + query if query else ''}"
        )
        http = self.http_get or _http_get
        try:
            response = http(url, self._headers())
        except urllib.error.HTTPError as exc:
            raise GitHubPRCommentChannelError(
                self._describe(exc, "polling for a reply")
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubPRCommentChannelError(
                self._scrub(f"could not reach {self.api_base}: {exc.reason}")
            ) from exc
        return response if isinstance(response, list) else []

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _describe(self, exc: urllib.error.HTTPError, action: str) -> str:
        """A helpful, token-free message from a GitHub error response."""

        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = str(payload.get("message") or "")
        except (ValueError, OSError):
            detail = ""
        if exc.code in (401, 403):
            detail = detail or "authentication failed — check the token's pull-request scope"
        return self._scrub(f"GitHub returned {exc.code} {action}: {detail}".rstrip(": "))

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text
