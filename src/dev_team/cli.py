"""Command-line interface for the dev-team system."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import List, Mapping, Optional

from . import __version__
from .assessment import (
    ASSESSMENT_JSON_PATH,
    AssessConfig,
    dict_to_backlog,
    find_finding,
    outcome_to_dict,
    verify_finding,
)
from .backlog import BacklogStore
from .budget import Budget
from .chat import ChatSession, chat_system_prompt
from .config import TeamConfig
from .dashboard import DashboardServer
from .dispatch import DispatchServer
from .engine import EngineConfig
from .errors import DevTeamError
from .eventlog import EventLog, compose
from .events import AgentEvent, Listener
from .execution import DEFAULT_EXCLUDED_DIRS, LocalWorkspace, SubprocessCommandRunner
from .interaction import ChannelApprovalGate, ConsoleChannel
from .models import FeatureRequest
from .persona import Roster
from .report import (
    delivery_to_dict,
    render_delivery_summary,
    render_summary,
    result_to_dict,
)
from .sdk import AgentRunner, ChatBackend, ClaudeAgentRunner, ClaudeChatBackend
from .sources import (
    clone_or_update,
    default_env_file,
    parse_repo,
    resolve_github_token,
)
from .team import DevTeam
from .transcripts import TRANSCRIPTS_DIR, TranscriptRecorder

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

#: Where the dashboard's board-write proxy sends /api/backlog/* edits when
#: --dispatch-url is not given: the dispatch service's default local bind.
DEFAULT_DISPATCH_URL = "http://127.0.0.1:8738"

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
        epilog=(
            "exit codes:\n"
            "  0    success\n"
            "  1    completed with failed tasks\n"
            "  2    invalid input or usage error\n"
            "  130  interrupted (Ctrl-C)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "title",
        nargs="?",
        default=None,
        help="Short title of the feature to build (omit with --chat).",
    )
    parser.add_argument(
        "description",
        nargs="?",
        default=None,
        help="Detailed description of what the feature should do (omit with --chat).",
    )

    # Argument groups organise --help only; they do not change parsing, so the
    # add order below (and every flag's dest/default) is unchanged.
    modes = parser.add_argument_group(
        "modes",
        "What dev-team does. With no mode flag it runs the paid simulation on "
        "the title/description; the flags here select other modes.",
    )
    delivery = parser.add_argument_group(
        "delivery options", "Tune real delivery (with --deliver)."
    )
    assessment = parser.add_argument_group(
        "assessment options",
        "Tune an audit and its follow-ups (with --assess/--verify).",
    )
    serving = parser.add_argument_group(
        "serving options", "Bind the dashboard/dispatch services."
    )
    interaction = parser.add_argument_group(
        "interaction & personas", "Collaboration and the agent cast."
    )
    misc = parser.add_argument_group("general options")

    misc.add_argument(
        "-c",
        "--constraint",
        action="append",
        default=[],
        dest="constraints",
        metavar="TEXT",
        help="A constraint the solution must satisfy (repeatable).",
    )
    misc.add_argument("--model", default=None, help="Model id for the agents.")
    misc.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="Maximum attempts per task before it is marked failed.",
    )
    misc.add_argument(
        "--min-coverage",
        type=float,
        default=80.0,
        help="Minimum test coverage percent QA must report for a task to pass "
        "in the default simulation mode (default 80; 100 is strict and tends "
        "to mark first runs INCOMPLETE). Ignored by --deliver, which gates on "
        "its definition-of-done instead — passing both is an error.",
    )
    interaction.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Collaborate with the run: review/revise the plan before work "
        "starts, decide what happens when a task fails, and approve the "
        "feature commit and risky commands on this terminal.",
    )
    modes.add_argument(
        "--chat",
        action="store_true",
        help="Start a conversation with the product manager to shape the "
        "feature first; /run or /deliver hands the agreed brief to the team.",
    )
    interaction.add_argument(
        "--roster",
        default=None,
        metavar="FILE",
        help="JSON file customising agent personas, e.g. "
        '{"engineer": {"name": "Ada", "style": "..."}}. '
        "Entries overlay the default cast.",
    )
    interaction.add_argument(
        "--no-personas",
        action="store_true",
        help="Disable agent personas; agents present as bare roles.",
    )
    modes.add_argument(
        "--deliver",
        action="store_true",
        help="Run the real delivery engine (writes files, runs gates, commits) "
        "instead of the default simulation. Note the simulation is not free: it "
        "runs the same real, paid agents, it just makes no filesystem or git "
        "changes.",
    )
    modes.add_argument(
        "--assess",
        action="store_true",
        help="Audit the --workspace repository read-only (inventory, "
        "buildability, risk, tests/docs, recommendation) and write a cited "
        "markdown report. Optional description argument scopes the audit.",
    )
    modes.add_argument(
        "--dashboard",
        action="store_true",
        help="Serve a local web dashboard over the --workspace: each "
        "agent's last activity, recent runs, the backlog, cross-run memory, "
        "and assessment reports. Read-only; runs happily alongside "
        "--deliver/--assess runs on the same workspace.",
    )
    modes.add_argument(
        "--dispatch",
        action="store_true",
        help="Serve an authenticated HTTP dispatch service: an external "
        "caller can submit assess/deliver jobs against a repository, poll "
        "status, and fetch results. Jobs run one at a time (single-flight). "
        "The bearer token is read from the DEV_TEAM_DISPATCH_TOKEN "
        "environment variable; bind to a trusted (e.g. tailnet) address.",
    )
    serving.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for --dashboard (default 8737) or --dispatch (default 8738).",
    )
    serving.add_argument(
        "--host",
        default=None,
        metavar="ADDR",
        help="Bind address for --dashboard or --dispatch (default 127.0.0.1). "
        "The dashboard can read any file the workspace holds — set "
        "DEV_TEAM_DASHBOARD_TOKEN when widening the bind (unauthenticated "
        "non-local binds get a stderr warning); the dispatch service "
        "authenticates but runs agent code — only widen this on a trusted "
        "network.",
    )
    serving.add_argument(
        "--dashboard-workspace",
        default=None,
        metavar="DIR",
        help="With --dispatch: a shared workspace that a separate "
        "`--dashboard` process watches. Each dispatched job also journals "
        "its events here (shown as its own run) and mirrors its assess "
        "report under audit/<job-id>/, so dispatched runs are visible on the "
        "dashboard. Each job still runs in its own isolated workspace.",
    )
    serving.add_argument(
        "--dispatch-url",
        default=None,
        metavar="URL",
        help="With --dashboard: base URL of the dispatch service the board's "
        f"write actions (/api/backlog/*) are proxied to (default "
        f"{DEFAULT_DISPATCH_URL}). The proxy authenticates with the "
        "DEV_TEAM_DISPATCH_TOKEN environment variable; without that token "
        "the board stays read-only (writes answer 501).",
    )
    assessment.add_argument(
        "--report",
        default=None,
        metavar="FILE",
        help="Workspace-relative path for the assessment report "
        "(with --assess; default: audit/assessment.md).",
    )
    assessment.add_argument(
        "--exclude",
        action="append",
        default=None,
        dest="exclude_globs",
        metavar="GLOB",
        help="Exclude paths matching this glob from the assessment "
        "(repeatable; replaces the built-in vendored/build-output defaults).",
    )
    assessment.add_argument(
        "--max-tree-entries",
        type=int,
        default=None,
        metavar="N",
        help="How many file-tree entries the assessment evidence may list "
        "(with --assess; default 400).",
    )
    assessment.add_argument(
        "--component-fanout",
        action="store_true",
        help="Deep-dive each detected sub-project with its own parallel "
        "audit (with --assess).",
    )
    assessment.add_argument(
        "--no-osv-scan",
        action="store_true",
        help="Skip the live OSV.dev vulnerability scan of pinned "
        "dependencies (with --assess). The scan is ON by default; when it "
        "runs, each pinned dependency's NAME and version is sent to "
        "api.osv.dev to look up known vulnerabilities. Pass this to keep the "
        "dependency list off the network.",
    )
    assessment.add_argument(
        "--no-eol-scan",
        action="store_true",
        help="Skip the live endoflife.date EOL/support-status check of "
        "detected runtime versions (with --assess). The scan is ON by "
        "default; when it runs, each detected runtime's product and version "
        "(parsed from package.json/.nvmrc/runtime.txt/.python-version/"
        "global.json) is checked against endoflife.date. Pass this to keep "
        "the runtime list off the network.",
    )
    assessment.add_argument(
        "--backlog",
        action="store_true",
        help="Convert assessment findings into stories in the persistent "
        "backlog (.dev_team/backlog.json) so delivery runs can work them "
        "off (with --assess).",
    )
    modes.add_argument(
        "--make-backlog",
        default=None,
        metavar="DIR",
        help="Convert the assessment persisted in DIR "
        "(.dev_team/assessment.json, written by every --assess run) into "
        "stories in DIR/.dev_team/backlog.json, deduplicated by title. A "
        "pure local transform: no agents run, no credentials are needed, "
        "and it costs $0 — assess once, generate the backlog any time "
        "later. Standalone: not combined with other modes.",
    )
    modes.add_argument(
        "--verify",
        default=None,
        metavar="DIR",
        help="Re-check ONE finding of the assessment persisted in DIR "
        "(.dev_team/assessment.json, written by every --assess run) against "
        "the code: a fresh skeptical agent with read-only tools reads the "
        "cited files, hunts for contradicting evidence, and answers "
        "confirmed/refuted/needs-context with citations. Requires --finding "
        "and Claude credentials (an agent runs). Standalone: not combined "
        "with other modes.",
    )
    assessment.add_argument(
        "--finding",
        default=None,
        metavar="ID",
        help="Which finding --verify re-checks: a finding id from the "
        "persisted assessment (e.g. 'risk.secrets[0]') or a case-insensitive "
        "substring of its claim text (first match wins).",
    )
    assessment.add_argument(
        "--no-conventions",
        action="store_true",
        help="Do not persist the captured house-conventions profile "
        "(with --assess).",
    )
    assessment.add_argument(
        "--build-probe",
        action="store_true",
        help="Actually run the detected setup/verify commands so the "
        "buildability verdict rests on real exit codes (with --assess). "
        "This executes the repository's own build — arbitrary code — so "
        "only use it on trusted repos or inside a sandbox.",
    )
    misc.add_argument(
        "--workspace",
        default="./build",
        metavar="DIR",
        help="Working/target directory for --deliver (the delivery engine's "
        "build dir, created if missing), --assess (the repository to audit), "
        "and --dashboard (the workspace to serve). Default ./build.",
    )
    misc.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/NAME",
        help="Clone this GitHub repository (owner/name or a git URL) and use "
        "the clone as the workspace (with --assess, --deliver, or --chat). "
        "Private repositories authenticate with a GITHUB_TOKEN/GH_TOKEN found "
        "automatically in ./.env, ~/.config/dev-team/dev-team.env, or "
        "/etc/dev-team/dev-team.env (override with --env-file). An existing "
        "clone is fast-forwarded instead of re-cloned.",
    )
    misc.add_argument(
        "--env-file",
        default=None,
        metavar="FILE",
        help="KEY=VALUE file holding GITHUB_TOKEN/GH_TOKEN for --repo, "
        "overriding the default search (./.env, "
        "~/.config/dev-team/dev-team.env, /etc/dev-team/dev-team.env). "
        "The token stays out of the process environment, so commands the "
        "agents run never see it.",
    )
    delivery.add_argument(
        "--verify-command",
        default=None,
        metavar="CMD",
        help="Quality-gate command run in the workspace (with --deliver). "
        "Defaults to auto-detection from the workspace's manifests.",
    )
    delivery.add_argument(
        "--setup-command",
        default=None,
        metavar="CMD",
        help="Command run once in the workspace before delivery starts, "
        "e.g. 'npm install' (with --deliver).",
    )
    delivery.add_argument(
        "--remote-verify-status",
        default=None,
        metavar="CMD",
        help="Delegate verification to an external CI system: this command "
        "is polled until it exits zero, e.g. a pipeline status check "
        "(with --deliver). For stacks that cannot build locally.",
    )
    delivery.add_argument(
        "--remote-verify-trigger",
        default=None,
        metavar="CMD",
        help="Command that kicks off the remote CI run before polling "
        "--remote-verify-status (with --deliver).",
    )
    delivery.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help="Branch the delivery works on (default: dev-team/<feature-slug>).",
    )
    delivery.add_argument(
        "--allow-dirty-baseline",
        action="store_true",
        help="Proceed over uncommitted changes by sweeping them into a "
        "baseline commit on the delivery branch (with --deliver).",
    )
    delivery.add_argument(
        "--proceed-on-red-baseline",
        action="store_true",
        help="Start even when the workspace's quality gates already fail "
        "(with --deliver). By default a red baseline halts the run.",
    )
    misc.add_argument(
        "--budget-usd",
        type=float,
        default=None,
        metavar="USD",
        help="Cost ceiling for the run; the run stops gracefully when reached "
        "(with --deliver).",
    )
    delivery.add_argument(
        "--max-concurrency",
        type=int,
        default=4,
        help="How many independent tasks may be implemented at once (with --deliver).",
    )
    delivery.add_argument(
        "--no-commit",
        action="store_true",
        help="Do not git-commit the delivered work (with --deliver).",
    )
    misc.add_argument(
        "--json",
        action="store_true",
        help="Emit the result as JSON instead of a text summary.",
    )
    misc.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print the full progress event stream as the team works.",
    )
    misc.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the default one-line-per-event progress shown on "
        "--deliver/--assess. No effect with -v/--verbose (which prints the "
        "full event stream).",
    )
    misc.add_argument(
        "--record-transcripts",
        action="store_true",
        help="Capture each agent call's raw system prompt, prompt, response "
        "and cost under .dev_team/transcripts/ so they show in the dashboard's "
        "agent modal (with --assess/--deliver/--dispatch). OFF by default. "
        "Transcripts contain raw repository content (including any secrets in "
        "the repo) and the dashboard is unauthenticated — only enable on a "
        "trusted (tailnet) network. The dispatch service also honours the "
        "DEV_TEAM_RECORD_TRANSCRIPTS environment variable.",
    )
    return parser


def _validate_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Reject argument combinations that would silently misbehave."""

    if args.chat and args.assess:
        parser.error("--chat and --assess are mutually exclusive")
    if args.assess and args.deliver:
        parser.error("--assess is read-only; it cannot be combined with --deliver")
    if args.dashboard and (args.assess or args.deliver or args.chat):
        parser.error(
            "--dashboard is a viewer; run it as its own process alongside "
            "--assess/--deliver/--chat runs"
        )
    if args.dispatch and (args.assess or args.deliver or args.chat or args.dashboard):
        parser.error(
            "--dispatch is a standalone service; run it as its own process, "
            "not alongside --assess/--deliver/--chat/--dashboard"
        )
    if args.make_backlog is not None and (
        args.assess or args.deliver or args.chat or args.dashboard or args.dispatch
    ):
        parser.error(
            "--make-backlog is a standalone offline transform; it cannot be "
            "combined with --assess/--deliver/--chat/--dashboard/--dispatch"
        )
    if args.verify is not None and (
        args.assess or args.deliver or args.chat or args.dashboard
        or args.dispatch or args.make_backlog is not None
    ):
        parser.error(
            "--verify re-checks one persisted finding; it cannot be combined "
            "with --assess/--deliver/--chat/--dashboard/--dispatch/"
            "--make-backlog"
        )
    if args.verify is not None and args.finding is None:
        parser.error("--verify requires --finding (a finding id or claim substring)")
    if args.finding is not None and args.verify is None:
        parser.error("--finding: only valid with --verify")
    if args.port is not None and not (args.dashboard or args.dispatch):
        parser.error("--port: only valid with --dashboard or --dispatch")
    if args.host is not None and not (args.dashboard or args.dispatch):
        parser.error("--host: only valid with --dashboard or --dispatch")
    if args.dashboard_workspace is not None and not args.dispatch:
        parser.error("--dashboard-workspace: only valid with --dispatch")
    if args.dispatch_url is not None and not args.dashboard:
        parser.error("--dispatch-url: only valid with --dashboard")
    if args.report is not None and not args.assess:
        parser.error("--report: only valid with --assess")
    if args.record_transcripts and not (
        args.assess or args.deliver or args.dispatch
    ):
        # --chat is deliberately excluded: a chat has no run id / event log to
        # correlate transcripts with, so recording there captures nothing.
        parser.error(
            "--record-transcripts: only valid with --assess/--deliver/--dispatch"
        )
    if args.chat:
        if args.title is not None or args.description is not None:
            parser.error("--chat shapes the feature in conversation; omit "
                         "the title/description arguments")
        if args.json:
            parser.error("--json: not available with --chat (stdout is the conversation)")
    elif not (
        args.assess or args.dashboard or args.dispatch
        or args.make_backlog is not None or args.verify is not None
    ):
        if args.title is None or args.description is None:
            parser.error("title and description are required (or use --chat)")
    if args.roster is not None and args.no_personas:
        parser.error("--roster and --no-personas are mutually exclusive")
    if args.repo is not None and not (args.assess or args.deliver or args.chat):
        parser.error("--repo: only valid with --assess, --deliver, or --chat")
    if args.env_file is not None and args.repo is None:
        parser.error("--env-file: only valid with --repo")
    if args.remote_verify_trigger is not None and args.remote_verify_status is None:
        parser.error("--remote-verify-trigger requires --remote-verify-status")
    if not args.assess:
        assess_only = [
            ("--exclude", args.exclude_globs is not None),
            ("--max-tree-entries", args.max_tree_entries is not None),
            ("--component-fanout", args.component_fanout),
            ("--no-osv-scan", args.no_osv_scan),
            ("--no-eol-scan", args.no_eol_scan),
            ("--backlog", args.backlog),
            ("--no-conventions", args.no_conventions),
            ("--build-probe", args.build_probe),
        ]
        passed = [flag for flag, is_set in assess_only if is_set]
        if passed:
            parser.error(f"{', '.join(passed)}: only valid with --assess")
    if args.deliver and args.min_coverage != parser.get_default("min_coverage"):
        # --min-coverage is a simulation-mode QA gate; the delivery engine has
        # no such field and would silently ignore it. Point at the model that
        # actually gates a delivery so the coverage intent is not lost.
        parser.error(
            "--min-coverage is a simulation-mode QA gate and is ignored by "
            "--deliver, which gates on its definition-of-done "
            "(--verify-command / --remote-verify-status) instead"
        )
    if args.budget_usd is not None and (
        args.dashboard or args.dispatch or args.make_backlog is not None
    ):
        # A cost ceiling only means something when metered agents run; these
        # modes run none (or, for --dispatch, meter each job separately).
        parser.error(
            "--budget-usd: no metered agents run in "
            "--dashboard/--dispatch/--make-backlog, so a cost ceiling has "
            "nothing to enforce"
        )
    # Read/consume modes must point at a directory that already exists.
    # LocalWorkspace would otherwise mkdir a mistyped path and then present an
    # empty dashboard or a misleading "no assessment" error that hides the typo.
    if args.dashboard and not os.path.isdir(args.workspace):
        parser.error(f"--dashboard: no such directory: {args.workspace}")
    if args.verify is not None and not os.path.isdir(args.verify):
        parser.error(f"--verify: no such directory: {args.verify}")
    if args.make_backlog is not None and not os.path.isdir(args.make_backlog):
        parser.error(f"--make-backlog: no such directory: {args.make_backlog}")
    _reject_deliver_only_flags(parser, args)
    # --assess audits an existing repository read-only; a missing or empty
    # --workspace is a typo (LocalWorkspace would mkdir it and then silently
    # audit an empty tree). --repo is exempt — its clone lands here later.
    if args.assess and args.repo is None and (
        not os.path.isdir(args.workspace) or not os.listdir(args.workspace)
    ):
        parser.error(
            f"--assess audits an existing repository, but {args.workspace} "
            "does not exist or is empty; pass an existing --workspace (or "
            "--repo to clone one)"
        )


def _reject_deliver_only_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    """Error out (exit code 2) on deliver-only flags passed without --deliver.

    These flags only affect the delivery engine; silently ignoring them in
    simulation mode would let e.g. ``--no-commit`` go unheeded. A chat session
    may end in ``/deliver``, so chat mode accepts them too. (``--budget-usd``
    is NOT deliver-only — the simulation, assessment, and verification all run
    metered agents — so it is validated separately.)
    """

    if args.deliver or args.chat:
        return
    checks = [
        ("--verify-command", args.verify_command is not None),
        ("--setup-command", args.setup_command is not None),
        ("--branch", args.branch is not None),
        ("--allow-dirty-baseline", args.allow_dirty_baseline),
        ("--proceed-on-red-baseline", args.proceed_on_red_baseline),
        ("--max-concurrency", args.max_concurrency != parser.get_default("max_concurrency")),
        ("--no-commit", args.no_commit),
        ("--remote-verify-status", args.remote_verify_status is not None),
        ("--remote-verify-trigger", args.remote_verify_trigger is not None),
    ]
    if not (args.assess or args.dashboard):
        checks += [
            ("--workspace", args.workspace != parser.get_default("workspace")),
        ]
    passed = [flag for flag, is_set in checks if is_set]
    if passed:
        parser.error(f"{', '.join(passed)}: only valid with --deliver")


def _build_roster(args: argparse.Namespace) -> Optional[Roster]:
    """The roster implied by the flags (``None`` means the default cast)."""

    if args.no_personas:
        return Roster.anonymous()
    if args.roster is not None:
        return Roster.from_file(args.roster)
    return None


def _engine_config(args: argparse.Namespace) -> EngineConfig:
    return EngineConfig(
        model=args.model,
        max_task_attempts=args.max_attempts,
        max_concurrency=args.max_concurrency,
        verify_command=(
            tuple(shlex.split(args.verify_command)) if args.verify_command else None
        ),
        setup_command=(
            tuple(shlex.split(args.setup_command)) if args.setup_command else None
        ),
        commit=not args.no_commit,
        branch=args.branch,
        allow_dirty_baseline=args.allow_dirty_baseline,
        require_green_baseline=not args.proceed_on_red_baseline,
        remote_verify_status=(
            tuple(shlex.split(args.remote_verify_status))
            if args.remote_verify_status
            else None
        ),
        remote_verify_trigger=(
            tuple(shlex.split(args.remote_verify_trigger))
            if args.remote_verify_trigger
            else None
        ),
    )


def _transcript_recorder(args, run: Optional[str]) -> Optional[TranscriptRecorder]:
    """A recorder tied to the run's event-log id, or ``None`` when disabled.

    The recorder MUST share the run id used for the :class:`EventLog` so the
    dashboard correlates a transcript with the agent's timeline.
    """

    if not args.record_transcripts or run is None:
        return None
    return TranscriptRecorder(LocalWorkspace(args.workspace), run=run)


def _progress_printer(budget: Budget) -> Listener:
    """A concise one-line-per-event progress display for stderr.

    The default feedback for --deliver/--assess when -v (the full event
    stream) is off and --quiet has not silenced it: an unattended run should
    still show which phase/agent is active and how much it has spent. The
    running cost is read live off the shared budget meter, so it climbs as the
    agents work.
    """

    def printer(event: AgentEvent) -> None:
        who = f"{event.name} ({event.role})" if event.name else event.role
        print(
            f"[{who}/{event.stage}] {event.message} (${budget.spent:.4f})",
            file=sys.stderr,
        )

    return printer


async def _deliver(
    team: DevTeam,
    request: FeatureRequest,
    args,
    run: Optional[str] = None,
    *,
    budget: Budget,
) -> int:
    """Run real delivery and print the result; returns the exit code."""

    kwargs = {}
    if team.interaction is not None:
        # Route the engine's approval points (feature commit, push/deploy/rm
        # commands) through the same conversation as plan review.
        kwargs["approval"] = ChannelApprovalGate(team.interaction)
    recorder = _transcript_recorder(args, run)
    if recorder is not None:
        kwargs["transcript_recorder"] = recorder
    outcome = await team.deliver(
        request,
        workspace=LocalWorkspace(args.workspace),
        budget=budget,
        config=_engine_config(args),
        **kwargs,
    )
    if args.json:
        print(json.dumps(delivery_to_dict(outcome), indent=2))
    else:
        print(render_delivery_summary(outcome))
    return 0 if outcome.success else 1


async def _assess(
    team: DevTeam, args, run: Optional[str] = None, *, budget: Budget
) -> int:
    """Run a read-only repository assessment; returns the exit code."""

    focus_parts = [p for p in (args.title, args.description) if p]
    config_kwargs = {"model": args.model, "focus": " — ".join(focus_parts) or None}
    if args.report is not None:
        config_kwargs["report_path"] = args.report
    if args.exclude_globs is not None:
        config_kwargs["exclude_globs"] = tuple(args.exclude_globs)
    if args.max_tree_entries is not None:
        config_kwargs["max_tree_entries"] = args.max_tree_entries
    if args.component_fanout:
        config_kwargs["component_fanout"] = True
    if args.no_osv_scan:
        config_kwargs["osv_scan"] = False
    if args.no_eol_scan:
        config_kwargs["eol_scan"] = False
    if args.backlog:
        config_kwargs["update_backlog"] = True
    if args.no_conventions:
        config_kwargs["save_conventions"] = False
    if args.build_probe:
        config_kwargs["build_probe"] = True
    kwargs = {}
    recorder = _transcript_recorder(args, run)
    if recorder is not None:
        kwargs["transcript_recorder"] = recorder
    outcome = await team.assess(
        workspace=LocalWorkspace(args.workspace),
        budget=budget,
        config=AssessConfig(**config_kwargs),
        **kwargs,
    )
    if args.json:
        print(json.dumps(outcome_to_dict(outcome), indent=2))
    else:
        print(outcome.report_markdown)
    return 0 if outcome.success else 1


async def _simulate(
    team: DevTeam, request: FeatureRequest, args, *, budget: Budget
) -> int:
    """Run the simulation workflow and print the result; returns the exit code.

    "Simulation" means no filesystem/git side effects — it still drives the
    same real, paid agents. The shared budget is always handed to the workflow
    so it meters spend (surfaced as ``ProjectResult.cost_usd``); a
    ``--budget-usd`` ceiling additionally stops the run gracefully at the line.
    """

    # The default mode looks free but drives the same paid agents, so say so
    # once up front (stderr, out of the result document). --quiet silences it;
    # a single line means -v never double-prints it.
    if not args.quiet:
        print(
            "note: simulation runs real, paid agents (no files or git are "
            "changed); pass --budget-usd to cap spend.",
            file=sys.stderr,
        )
    result = await team.develop(request, budget=budget)
    if args.json:
        print(json.dumps(result_to_dict(result), indent=2))
    else:
        print(render_summary(result))
    return 0 if result.success else 1


async def _chat(
    team: DevTeam,
    args: argparse.Namespace,
    backend: Optional[ChatBackend],
    *,
    budget: Budget,
) -> int:
    """Run the ``--chat`` session; returns the last run's exit code."""

    pm_persona = team.roster.get("product-manager")
    if backend is None:
        backend = ClaudeChatBackend(
            system_prompt=chat_system_prompt(pm_persona), model=args.model
        )

    async def run_feature(request: FeatureRequest, deliver: bool) -> int:
        if deliver:
            return await _deliver(team, request, args, budget=budget)
        return await _simulate(team, request, args, budget=budget)

    session = ChatSession(
        backend=backend,
        run_feature=run_feature,
        pm_name=pm_persona.name if pm_persona is not None else "product-manager",
    )
    return await session.run()


def _materialise_repo(args, default_workspace: str) -> None:
    """Clone (or update) ``--repo`` and point ``--workspace`` at the result.

    The token is resolved from ``--env-file`` or, without one, the default
    search (``./.env``, then ``~/.config/dev-team/dev-team.env``, then
    ``/etc/dev-team/dev-team.env``) — set up once, never passed per run.
    Failing all of those it is taken *out of* the process environment — the
    engines' subprocesses must never inherit it. An explicit ``--workspace``
    is the clone destination; otherwise each repository gets its own
    directory under the default workspace root.
    """

    ref = parse_repo(args.repo)
    env_file = args.env_file if args.env_file is not None else default_env_file()
    token = resolve_github_token(env_file)
    via = f" (env file: {env_file})" if env_file is not None else ""
    if args.workspace == default_workspace:
        args.workspace = str(Path(default_workspace) / ref.workspace_name)
    print(f"fetching {ref.slug} into {args.workspace}{via}", file=sys.stderr)
    clone_or_update(
        ref, args.workspace, runner=SubprocessCommandRunner(), token=token
    )


#: The dashboard lists ``.dev_team/transcripts/`` to surface transcripts, so
#: (unlike prompt-facing listings) it must NOT exclude ``.dev_team`` — every
#: other heavy/vendored directory stays excluded.
_DASHBOARD_EXCLUDED_DIRS = DEFAULT_EXCLUDED_DIRS - {".dev_team"}


#: Environment variable holding the dashboard's (opt-in) access token.
#: When set, every dashboard route requires it — as a bearer header or via
#: the browser login form (see ``dashboard.py``). Rotate by changing the
#: value and restarting. Empty/unset keeps the dashboard open (localhost
#: dev); this is a stopgap until an IdP (Auth0) integration lands.
DASHBOARD_TOKEN_ENV = "DEV_TEAM_DASHBOARD_TOKEN"


def _serve_dashboard(args) -> int:
    """Serve the workspace dashboard until interrupted; returns exit code.

    Unlike --dispatch, a missing token is not an error (localhost dev must
    keep working), but binding beyond loopback without one earns a stderr
    warning: the workspace — including any recorded transcripts — would be
    readable by anyone who can reach the port.
    """

    token = os.environ.get(DASHBOARD_TOKEN_ENV, "")
    host = args.host if args.host is not None else "127.0.0.1"
    workspace = LocalWorkspace(args.workspace, excluded_dirs=_DASHBOARD_EXCLUDED_DIRS)
    if not token and host not in ("127.0.0.1", "localhost"):
        print(
            f"WARNING: the dashboard on {host} is UNAUTHENTICATED - anyone "
            "who can reach it can read the whole workspace (events, reports, "
            f"transcripts). Set ${DASHBOARD_TOKEN_ENV} to require a token.",
            file=sys.stderr,
        )
    elif not token and workspace.exists(TRANSCRIPTS_DIR):
        # Even bound to loopback, an unauthenticated dashboard over a workspace
        # that already holds recorded transcripts exposes raw prompts and model
        # replies (which can echo repository secrets). Warn, but don't hard-fail
        # — localhost dev must keep working (see the module note on the token).
        print(
            f"WARNING: {args.workspace} holds recorded agent transcripts and "
            f"the dashboard is UNAUTHENTICATED - anyone who can reach it can "
            "read raw agent prompts/responses (which may echo repository "
            f"secrets). Set ${DASHBOARD_TOKEN_ENV} to require a token.",
            file=sys.stderr,
        )
    # The board's write path: /api/backlog/* edits are proxied to the
    # dispatch service, authenticated with ITS bearer token. An unset/empty
    # dispatch token (localhost dev without a dispatch service) leaves the
    # board read-only — the proxy answers 501 rather than forwarding
    # unauthenticated. The token value itself is never printed.
    dispatch_token = os.environ.get(DISPATCH_TOKEN_ENV, "")
    server = DashboardServer(
        workspace,
        host=host,
        port=args.port if args.port is not None else 8737,
        token=token or None,
        dispatch_url=(
            args.dispatch_url if args.dispatch_url is not None else DEFAULT_DISPATCH_URL
        ),
        dispatch_token=dispatch_token or None,
    )
    print(
        f"dev-team dashboard for {args.workspace} at {server.url} "
        "(Ctrl-C to stop)",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def _make_backlog(args) -> int:
    """Generate backlog stories from a persisted assessment; returns exit code.

    Reads ``<DIR>/.dev_team/assessment.json`` (written by every ``--assess``
    run) and merges its findings into ``<DIR>/.dev_team/backlog.json`` — the
    same transform ``--assess --backlog`` applies inline, decoupled so an
    operator can assess once and generate (or refresh) the backlog any time
    later. No agents run and no credentials are needed: this is a pure disk
    transform that costs $0.
    """

    workspace = LocalWorkspace(args.make_backlog)
    if not workspace.exists(ASSESSMENT_JSON_PATH):
        raise DevTeamError(
            f"no assessment.json in {args.make_backlog}/.dev_team — "
            "run --assess there first"
        )
    data = json.loads(workspace.read_text(ASSESSMENT_JSON_PATH))
    # When job metadata sits beside the assessment (a dispatch-produced
    # workspace), the stories get a per-repository epic and finding
    # provenance; without it the historical single-epic behaviour holds.
    repo = source_job = None
    if workspace.exists(".dev_team/meta.json"):
        meta = json.loads(workspace.read_text(".dev_team/meta.json"))
        repo = meta.get("repo")
        source_job = meta.get("id")
    store = BacklogStore(workspace)
    backlog = store.load()
    added = dict_to_backlog(data, backlog, repo=repo, source_job=source_job)
    store.save(backlog)
    if args.json:
        print(
            json.dumps(
                {
                    "stories_added": len(added),
                    "stories_total": len(backlog.stories),
                },
                indent=2,
            )
        )
    else:
        print(
            f"{len(added)} story(ies) added; "
            f"{len(backlog.stories)} total in {store.path}"
        )
    return 0


async def _verify_one(args, runner: AgentRunner) -> int:
    """Re-check one persisted assessment finding; returns the exit code.

    Reads ``<DIR>/.dev_team/assessment.json`` (the ``--assess`` output),
    resolves ``--finding`` by id or claim substring, and has a FRESH
    skeptical agent — read-only tools, rooted at the assessed clone — try to
    refute the claim. Unlike ``--make-backlog`` this RUNS AN AGENT, so it
    sits behind the credential preflight and accepts ``--budget-usd``.

    Exit code ``0``: a verdict was produced (``refuted`` is a *successful*
    verification); ``1``: the verifier itself failed (budget, unusable
    response); ``2`` (raised): no persisted assessment or no matching
    finding.
    """

    workspace = LocalWorkspace(args.verify)
    if not workspace.exists(ASSESSMENT_JSON_PATH):
        raise DevTeamError(
            f"no assessment.json in {args.verify}/.dev_team — "
            "run --assess there first"
        )
    data = json.loads(workspace.read_text(ASSESSMENT_JSON_PATH))
    finding = find_finding(data, args.finding)
    if finding is None:
        raise DevTeamError(
            f"no finding matches {args.finding!r}; pass an id like "
            "'risk.secrets[0]' or a substring of the claim text"
        )
    result = await verify_finding(
        runner, workspace, finding, budget=Budget(limit_usd=args.budget_usd)
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["success"] else 1
    if not result["success"]:
        print(f"verification failed: {result['error']}")
        return 1
    print(f"{finding['id']} — {result['verdict']}")
    print(f"claim: {finding['claim']}")
    if result["rationale"]:
        print(f"rationale: {result['rationale']}")
    for citation in result["citations"]:
        print(f"  - {citation['path']}: {citation['note']}")
    print(f"cost: ${result['cost_usd']:.4f}")
    return 0


#: Environment variable holding the dispatch service's bearer token.
DISPATCH_TOKEN_ENV = "DEV_TEAM_DISPATCH_TOKEN"

#: Environment variable that (truthily) enables transcript recording for the
#: dispatch service — the box sets this in the unit's EnvironmentFile rather
#: than passing --record-transcripts, keeping the shipped unit OFF by default.
RECORD_TRANSCRIPTS_ENV = "DEV_TEAM_RECORD_TRANSCRIPTS"


def _env_truthy(value: Optional[str]) -> bool:
    """Whether an env var string reads as on (``1``/``true``/``yes``)."""

    return (value or "").strip().lower() in ("1", "true", "yes")


def _serve_dispatch(args, runner: Optional[AgentRunner]) -> int:
    """Serve the authenticated HTTP dispatch service until interrupted.

    The bearer token comes from :data:`DISPATCH_TOKEN_ENV`; a missing/empty
    token is a hard error (the service must never run unauthenticated). The
    default bind is localhost — the systemd unit widens it to the tailnet IP.
    """

    token = os.environ.get(DISPATCH_TOKEN_ENV, "")
    if not token:
        raise DevTeamError(
            f"--dispatch requires a bearer token in ${DISPATCH_TOKEN_ENV}; "
            "set it in the environment (or the service's EnvironmentFile) and "
            "share it only with the authorised caller"
        )
    server = DispatchServer(
        token,
        host=args.host if args.host is not None else "127.0.0.1",
        port=args.port if args.port is not None else 8738,
        runner=runner,
        dashboard_workspace=(
            LocalWorkspace(args.dashboard_workspace)
            if args.dashboard_workspace is not None
            else None
        ),
        record_transcripts=(
            args.record_transcripts
            or _env_truthy(os.environ.get(RECORD_TRANSCRIPTS_ENV))
        ),
    )
    print(
        f"dev-team dispatch service at {server.url} (Ctrl-C to stop)",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def _run(
    argv: Optional[List[str]],
    runner: Optional[AgentRunner],
    chat_backend: Optional[ChatBackend],
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    if args.dashboard:
        # A read-only viewer: no agents run, so no Claude credentials needed.
        return _serve_dashboard(args)

    if args.make_backlog is not None:
        # A pure disk transform over a persisted assessment: no agents run,
        # so no Claude credentials (and no repo/team/runner) are needed.
        return _make_backlog(args)

    if runner is None:
        # Only the real SDK runner needs credentials; an injected runner
        # (tests, embedding) brings its own transport.
        ensure_credentials()

    if args.dispatch:
        # This process runs real agents for submitted jobs, so credentials are
        # required (checked above unless a runner was injected).
        return _serve_dispatch(args, runner)

    if args.verify is not None:
        # Re-verification runs a real agent (hence the credential preflight
        # above), but needs no DevTeam — one fresh skeptical verifier.
        return asyncio.run(
            _verify_one(args, runner or ClaudeAgentRunner(default_model=args.model))
        )

    if args.repo is not None:
        _materialise_repo(args, parser.get_default("workspace"))

    config = TeamConfig(
        model=args.model,
        max_task_attempts=args.max_attempts,
        min_coverage=args.min_coverage,
    )

    # One meter for the whole run: the CLI stop-line (--budget-usd), the cost
    # the summary prints, and the live cost the default progress printer shows
    # all read from it. Uncapped (limit_usd=None) still meters spend.
    budget = Budget(limit_usd=args.budget_usd)

    # Progress goes to stderr so stdout stays a clean result document (text
    # summary or JSON), safe to pipe into e.g. ``jq``. -v prints the full event
    # stream; without it, --deliver/--assess still show a lightweight
    # running-cost line unless --quiet silences it. The two are mutually
    # exclusive, so no event is ever printed twice.
    printer = None
    if args.verbose:
        def printer(event: AgentEvent) -> None:  # noqa: E306
            print(str(event), file=sys.stderr)
    elif not args.quiet and (args.deliver or args.assess):
        printer = _progress_printer(budget)

    event_log = None
    if args.deliver or args.assess:
        # Journal progress into the workspace so `dev-team --dashboard`
        # (a separate process) can show what every agent is doing.
        mode = "deliver" if args.deliver else "assess"
        event_log = EventLog(
            LocalWorkspace(args.workspace),
            run=f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}",
        )
    listener = compose(printer, event_log)

    team = DevTeam(
        runner,
        config=config,
        listener=listener,
        roster=_build_roster(args),
        interaction=ConsoleChannel() if args.interactive else None,
    )

    # The recorder must journal transcripts under the SAME run id as the
    # events, so the dashboard can correlate them; that id lives on the
    # EventLog built above (None in modes that keep no event log).
    run_id = event_log.run if event_log is not None else None

    if args.chat:
        return asyncio.run(_chat(team, args, chat_backend, budget=budget))
    if args.assess:
        return asyncio.run(_assess(team, args, run_id, budget=budget))

    request = FeatureRequest(
        title=args.title,
        description=args.description,
        constraints=list(args.constraints),
    )
    if args.deliver:
        return asyncio.run(_deliver(team, request, args, run_id, budget=budget))
    return asyncio.run(_simulate(team, request, args, budget=budget))


def main(
    argv: Optional[List[str]] = None,
    runner: Optional[AgentRunner] = None,
    chat_backend: Optional[ChatBackend] = None,
) -> int:
    """CLI entry point. Returns a process exit code.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).
        runner: Optional runner override (used for testing); defaults to the
            real Claude Agent SDK runner.
        chat_backend: Optional chat backend override for ``--chat`` (used for
            testing); defaults to a persistent Claude session.
    """

    try:
        return _run(argv, runner, chat_backend)
    except KeyboardInterrupt:
        # A Ctrl-C during a paid run should not dump a raw asyncio traceback.
        print(
            "interrupted; any completed work is checkpointed and can be resumed",
            file=sys.stderr,
        )
        return 130
    except (DevTeamError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
