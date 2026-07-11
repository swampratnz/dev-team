"""Command-line interface for the dev-team system."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
from typing import List, Optional

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


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``dev-team`` command."""

    parser = argparse.ArgumentParser(
        prog="dev-team",
        description="Run a multi-agent software development team on a feature.",
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
        help="Cost ceiling for the run; the run stops gracefully when reached.",
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


def _run(argv: Optional[List[str]], runner: Optional[AgentRunner]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = TeamConfig(
        model=args.model,
        max_task_attempts=args.max_attempts,
        min_coverage=args.min_coverage,
    )

    listener = None
    if args.verbose:
        def listener(event: AgentEvent) -> None:  # noqa: E306
            print(str(event))

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
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
