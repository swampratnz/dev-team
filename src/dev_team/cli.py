"""Command-line interface for the dev-team system."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import List, Optional

from .config import TeamConfig
from .errors import DevTeamError
from .events import AgentEvent
from .report import render_summary, result_to_dict
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
        help="Minimum test coverage QA must report to pass a task.",
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
