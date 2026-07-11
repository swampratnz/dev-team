# Changelog

All notable changes to this project are documented in this file. Version
sections below are reconstructed from the repository history.

## [Unreleased]

### Interactivity & personas

#### Interactive runs
- **An `InteractionChannel` puts a human in the loop**: `--interactive` runs
  pause for plan review (approve / revise with feedback / abort) before any
  task work, escalate a task that exhausted its attempts (skip, or retry
  with guidance fed to the engineer as review feedback), and route the
  feature commit and policy-gated commands (`push`/`deploy`/`rm`) through
  interactive approval. Every question's default answer preserves the
  autonomous behaviour, EOF degrades to it, and resumed checkpoints don't
  re-litigate an already-approved plan. `QueueChannel` (thread-serviced,
  with timeout fallback) is the integration surface for external UIs;
  `ScriptedChannel` is the test double. See `docs/INTERACTION.md`.
- **`--chat` opens a conversation with the product manager** on a persistent
  `ClaudeSDKClient` session (context retained across turns) to shape the
  feature request before any run; `/run` / `/deliver` distil the
  conversation into the brief and hand it to the team, returning to the
  chat afterwards.

#### Personas
- **Every agent has a name**: a default cast (Priya, Anders, Sam, Rey,
  Quinn, Sasha, Wren, Riley, Devon) is injected additively into system
  prompts and carried on progress events (`[Priya (product-manager)/...]`).
  `--roster FILE` overlays custom names/styles (unknown roles rejected);
  `--no-personas` disables. Names are presentation only — checkpoints,
  memory, and events stay keyed by role.

### Review hardening

#### CLI & deployment
- **Claude subscription support is first-class**: the CLI now preflights
  credentials and accepts `CLAUDE_CODE_OAUTH_TOKEN` (a Pro/Max/Team/Enterprise
  subscription token from `claude setup-token`) alongside
  `ANTHROPIC_API_KEY`, a stored `claude` login, or gateway/Bedrock/Vertex
  variables — a missing credential now fails fast with guidance instead of
  surfacing as an opaque CLI error mid-run. `DEPLOYMENT.md` is now a full
  Ubuntu server install guide covering both auth options, and the systemd
  unit and Docker examples document the subscription token.

#### Delivery engine
- **Accepted work is banked**: every task that passes its gates is committed
  as a `wip(dev-team)` commit on the delivery branch, so a later task's
  rollback (hard reset) can no longer destroy earlier gated work, per-task
  diffs/reviews are no longer contaminated by prior tasks' changes, and the
  final feature commit is a soft-reset squash of the banked work.
- **Resume actually works**: checkpoints are per-feature files, carry the
  run's plan (reused on resume instead of gambling on a regenerated plan
  matching) and the original baseline sha (so the final squash spans the
  interrupted run's work). Corrupt checkpoint/memory/backlog files read as
  empty instead of crashing; `LocalWorkspace` writes are atomic
  (write-then-rename).
- `.dev_team/` is appended to an existing `.gitignore` (previously only
  written when none existed), so rollbacks no longer delete the engine's own
  checkpoint mid-run and leftovers no longer read as a dirty tree.
- Conflicted squash-merges in worktree mode are cleaned up and fed back to
  the engineer instead of poisoning the delivery branch for all later tasks;
  stale worktrees/branches from crashed runs are pruned and force-reset;
  `git stash` push/pop pairs are serialised across worktrees (the stash
  stack is repo-global).
- Non-agentic deliveries to a real directory get the same safeguards as
  agentic ones (dirty-tree halt, delivery branch, baseline commit); a
  workspace nested inside a larger repo no longer silently adopts the
  enclosing repo; git commands carry a timeout; duplicate task ids are
  renamed instead of crashing the scheduler after agent spend; planning or
  specialist-stage failures return a halted/partial outcome instead of
  unwinding the run.
- Security review evidence is reconciled against git so resumed tasks' files
  cannot be committed unseen.

#### Governance & measurement
- The approval gate is consulted before the feature commit; approval-token
  matching is by command position (`git push` gates, `git stash push` does
  not); a denied fail-to-pass stash is reported instead of silently skipped.
- A real workspace gets a persistent backlog by default, and reruns update
  the existing epic/stories instead of duplicating them per run.
- Project memory merges across runs (bounded) instead of each run erasing
  the last; ADR numbering continues across runs.
- The per-run scorecard is part of `DeliveryOutcome`, `--json` output, and
  the rendered summary.
- Evals: `max_cost_usd` makes cost part of a case's score; behavioural
  `check_commands` fail honestly on dry-run workspaces instead of vacuously
  passing; trace spans are closed when an agent call raises.

#### Agents & model I/O
- `extract_json` prefers the last JSON object in output (mid-task narration
  can no longer hijack the answer); non-object roots are rejected and
  retried; unknown severity strings fail **closed** (high/blocker block
  instead of downgrading to info).
- Non-engineer agents run with an explicit read-only tool allowlist rooted
  at the workspace (previously: unrestricted tools in the orchestrator's own
  cwd with edits auto-accepted).
- SDK calls carry a timeout and transient SDK errors are retried; untrusted
  content (file bodies, diffs, scanner output, prior-run memory) is fenced
  and marked as data-not-instructions in prompts; omitted/truncated review
  evidence is labelled; the JSON-retry prompt is self-contained.

#### Packaging & tooling
- Added a `LICENSE` file (MIT), `CHANGELOG.md`, and a `py.typed` marker.
- CLI: `--version` flag; deliver-only flags are rejected without `--deliver`
  (exit code 2); errors and `--verbose` progress go to stderr so `--json`
  output stays pipeable.
- Real-git integration tests for the `GitRepo` porcelain (which caught a
  real `rev-parse` misparse: failures echoed the ref name instead of "").
- Ruff lint gate (`make lint`, CI step); CI matrix extended to Python 3.13;
  git added to the container image and deployment prerequisites.

## [0.6.0]

Benchmark-grounded agents — fail-to-pass QA, SAST triage, budgeted review,
ADR-consistent design, INVEST plans, artifact-shipping specialists.

## [0.5.0]

Brownfield depth and parallel scale — repo context, baseline attribution,
per-task worktrees, retrospectives.

## [0.4.0]

Behave like a professional in real repos — baseline gates, delivery branches,
curated commits, diff-defined review.

## [0.3.0]

Make delivery real — agentic engineer, evidence-based review,
workspace-rooted gates.

## [0.2.0]

Real delivery engine: research-driven multi-agent capabilities.

## [0.1.0]

Initial multi-agent development team on the Claude Agent SDK; CI with a
least-privilege, matrixed, concurrency-controlled workflow.
