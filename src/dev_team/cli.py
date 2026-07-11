"""Command-line interface for the dev-team system."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import List, Mapping, Optional

from . import __version__
from .budget import Budget
from .config import TeamConfig
from .engine import EngineConfig
from .errors import DevTeamError
from .events import AgentEvent
from .execution import LocalWorkspace
from .models import FeatureRequest
from .report import (
    delivery_to_dict,
    render_delivery_summary,
    render_summary,
    result_to_dict,
)
from .sdk import AgentRunner
from .team import DevTeam


# Any one of these satisfies the credential preflight. The Claude CLI (which
# the Agent SDK spawns) resolves them itself; dev-team only checks presence so
# a missing credential fails fast with guidance instead of opaquely mid-run.
CREDENTIAL_ENV_VARS = (
    "CLAUDE_CODE_OAUTH_TOKEN",  # Claude subscription token from `claude setup-token`
    "ANTHROPIC_API_KEY",  # Claude API key (pay-as-you-go)
    "ANTHROPIC_AUTH_TOKEN",  # custom bearer token, e.g. an LLM gateway
    "CLAUDE_CODE_USE_BEDROCK",  # AWS Bedrock
    "CLAUDE_CODE_USE_VERTEX",  # Google Vertex AI
)

_MISSING_CREDENTIALS = """\
no Claude credentials found. The agents run via the Claude Code CLI, which
needs one of:
  - CLAUDE_CODE_OAUTH_TOKEN  a Claude subscription (Pro/Max) token; generate
                             one with `claude setup-token`
  - ANTHROPIC_API_KEY        a Claude API key (pay-as-you-go)
  - a stored login           run `claude` once interactively and log in
(ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_USE_BEDROCK, and CLAUDE_CODE_USE_VERTEX
are also honoured for gateway/Bedrock/Vertex setups.)"""


def ensure_credentials(
    environ: Optional[Mapping[str, str]] = None, home: Optional[Path] = None
) -> None:
    """Fail fast when the Claude CLI would find no credentials.

    Accepts any of :data:`CREDENTIAL_ENV_VARS` in ``environ`` (default
    ``os.environ``) or a stored interactive login at
    ``~/.claude/.credentials.json``. Raises :class:`DevTeamError` otherwise.
    """

    env = os.environ if environ is None else environ
    if any(env.get(name) for name in CREDENTIAL_ENV_VARS):
        return
    credentials_file = (home or Path.home()) / ".claude" / ".credentials.json"
    if credentials_file.is_file():
        return
    raise DevTeamError(_MISSING_CREDENTIALS)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``dev-team`` command."""

    parser = argparse.ArgumentParser(
        prog="dev-team",
        description="Run a multi-agent software development team on a feature.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("title", help="Short title of the feature to build.")
    parser.add_argument(
        "description",
        help="Detailed description of what the feature should do.",
    )
    parser.add_argument(
        "-c",
        "--constraint",
        action="append",
        default=[],
        dest="constraints",
        metavar="TEXT",
        help="A constraint the solution must satisfy (repeatable).",
    )
    parser.add_argument("--model", default=None, help="Model id for the agents.")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="Maximum attempts per task before it is marked failed.",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=100.0,
        help="Minimum test coverage QA must report to pass a task (simulation mode).",
    )
    parser.add_argument(
        "--deliver",
        action="store_true",
        help="Run the real delivery engine (writes files, runs gates, commits) "
        "instead of the side-effect-free simulation.",
    )
    parser.add_argument(
        "--workspace",
        default="./build",
        metavar="DIR",
        help="Directory the delivery engine works in (with --deliver).",
    )
    parser.add_argument(
        "--verify-command",
        default=None,
        metavar="CMD",
        help="Quality-gate command run in the workspace (with --deliver). "
        "Defaults to auto-detection from the workspace's manifests.",
    )
    parser.add_argument(
        "--setup-command",
        default=None,
        metavar="CMD",
        help="Command run once in the workspace before delivery starts, "
        "e.g. 'npm install' (with --deliver).",
    )
    parser.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help="Branch the delivery works on (default: dev-team/<feature-slug>).",
    )
    parser.add_argument(
        "--allow-dirty-baseline",
        action="store_true",
        help="Proceed over uncommitted changes by sweeping them into a "
        "baseline commit on the delivery branch (with --deliver).",
    )
    parser.add_argument(
        "--proceed-on-red-baseline",
        action="store_true",
        help="Start even when the workspace's quality gates already fail "
        "(with --deliver). By default a red baseline halts the run.",
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        metavar="USD",
        help="Cost ceiling for the run; the run stops gracefully when reached "
        "(with --deliver).",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=4,
        help="How many independent tasks may be implemented at once (with --deliver).",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Do not git-commit the delivered work (with --deliver).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON instead of a text summary.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print progress events as the team works.",
    )
    return parser


def _reject_deliver_only_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Error out (exit code 2) on deliver-only flags passed without --deliver.

    These flags only affect the delivery engine; silently ignoring them in
    simulation mode would let e.g. ``--budget-usd`` go unenforced.
    """

    if args.deliver:
        return
    passed = [
        flag
        for flag, is_set in (
            ("--workspace", args.workspace != parser.get_default("workspace")),
            ("--verify-command", args.verify_command is not None),
            ("--setup-command", args.setup_command is not None),
            ("--branch", args.branch is not None),
            ("--allow-dirty-baseline", args.allow_dirty_baseline),
            ("--proceed-on-red-baseline", args.proceed_on_red_baseline),
            ("--budget-usd", args.budget_usd is not None),
            ("--max-concurrency", args.max_concurrency != parser.get_default("max_concurrency")),
            ("--no-commit", args.no_commit),
        )
        if is_set
    ]
    if passed:
        parser.error(f"{', '.join(passed)}: only valid with --deliver")


def _run(argv: Optional[List[str]], runner: Optional[AgentRunner]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _reject_deliver_only_flags(parser, args)

    if runner is None:
        # Only the real SDK runner needs credentials; an injected runner
        # (tests, embedding) brings its own transport.
        ensure_credentials()

    config = TeamConfig(
        model=args.model,
        max_task_attempts=args.max_attempts,
        min_coverage=args.min_coverage,
    )

    listener = None
    if args.verbose:
        # Progress goes to stderr so stdout stays a clean result document
        # (text summary or JSON), safe to pipe into e.g. ``jq``.
        def listener(event: AgentEvent) -> None:  # noqa: E306
            print(str(event), file=sys.stderr)

    team = DevTeam(runner, config=config, listener=listener)

    if args.deliver:
        request = FeatureRequest(
            title=args.title,
            description=args.description,
            constraints=list(args.constraints),
        )
        outcome = asyncio.run(
            team.deliver(
                request,
                workspace=LocalWorkspace(args.workspace),
                budget=Budget(limit_usd=args.budget_usd),
                config=EngineConfig(
                    model=args.model,
                    max_task_attempts=args.max_attempts,
                    max_concurrency=args.max_concurrency,
                    verify_command=(
                        tuple(shlex.split(args.verify_command))
                        if args.verify_command
                        else None
                    ),
                    setup_command=(
                        tuple(shlex.split(args.setup_command))
                        if args.setup_command
                        else None
                    ),
                    commit=not args.no_commit,
                    branch=args.branch,
                    allow_dirty_baseline=args.allow_dirty_baseline,
                    require_green_baseline=not args.proceed_on_red_baseline,
                ),
            )
        )
        if args.json:
            print(json.dumps(delivery_to_dict(outcome), indent=2))
        else:
            print(render_delivery_summary(outcome))
        return 0 if outcome.success else 1

    result = asyncio.run(team.develop_feature(args.title, args.description, args.constraints))

    if args.json:
        print(json.dumps(result_to_dict(result), indent=2))
    else:
        print(render_summary(result))

    return 0 if result.success else 1


def main(argv: Optional[List[str]] = None, runner: Optional[AgentRunner] = None) -> int:
    """CLI entry point. Returns a process exit code.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).
        runner: Optional runner override (used for testing); defaults to the
            real Claude Agent SDK runner.
    """

    try:
        return _run(argv, runner)
    except (DevTeamError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
