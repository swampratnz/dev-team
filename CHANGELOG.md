# Changelog

All notable changes to this project are documented in this file. Version
sections below are reconstructed from the repository history.

## [Unreleased]

### Self-improvement pipeline
- **A supervised multi-loop development pipeline now extends this repo
  itself** (`docs/PIPELINE.md`), ported from the community-agent repo's
  battle-tested pipeline: scheduled research/adversarial/orchestrator
  routines coordinate through GitHub issue labels
  (`proposal`/`status:*`/`needs-human` — created by the idempotent
  `setup-labels` workflow + `scripts/setup-labels.sh`), and event-driven
  GitHub Actions do the code work — a build worker
  (`pipeline-build.yml`, fires on `status:approved`, runs the exact CI
  gate — `ruff check .` + the 100% branch-coverage `pytest` — before
  opening a "Closes #N" PR, with a deterministic PR-produced verify step),
  a read-only security-focused PR reviewer (`pipeline-pr-review.yml`,
  verdict posted deterministically from the execution log), and three
  bounded push-exception loops: autofix on CI failure
  (`pipeline-pr-autofix.yml`, from run_attempt ≥ 2, 2 attempts), a
  Changes-requested reviser (`pipeline-pr-revise.yml`, 2 attempts,
  dispatched by the reviewer), and a two-hop merge-conflict resolver
  (`pipeline-pr-conflict.yml`, one attempt, payload carries PR numbers
  only with eligibility re-verified from the API). `pipeline-build-retry`
  and `ci-retry` give failed runs bounded machine reruns before any agent
  or human is spent; everything else escalates `needs-human`.
- **Guardrails are structural**: least-privilege `--allowedTools` (exact
  `git push origin HEAD`, no blanket `git:*`/`gh:*`/`python:*`, no
  `gh pr merge`/`gh api`), `persist-credentials: false` + per-step
  GH_TOKEN so agents reading untrusted content never hold a repo token,
  fork PRs excluded everywhere, attempt caps via marker comments, and
  **no loop merges — humans merge** (reinforcing the BPG standards, which
  also gained a "Multi-loop pipeline" section in `CLAUDE.md`).
  `docs/VISION.md` defines the proposal rubric and the explicit
  do-not-propose list (no merge autonomy, no credential-handling changes,
  no weakening the coverage gate). All agent workflows are inert until the
  `CLAUDE_CODE_OAUTH_TOKEN` secret and Claude GitHub App exist.

### Finding re-verification
- **A fresh skeptical agent can re-check any ONE persisted assessment
  finding** against the code (`docs/ASSESSMENT.md`): `list_findings`
  enumerates the LLM phases' claims from `.dev_team/assessment.json` with
  positional ids (`risk.secrets[0]`; component deep-dives nest), and
  `verify_finding` runs a security-engineer-disciplined verifier — never
  the claim's author — with read-only tools (`Read`/`Grep`/`Glob`) and
  refute-first instructions, returning exactly
  `confirmed|refuted|needs-context` plus rationale and citations. The
  claim under review is treated as untrusted (delimited) content;
  out-of-contract verdicts degrade to `needs-context`. Deterministic
  `dead_code`/`dependency_scan` outputs are excluded — they are program
  results, not model claims.
- **CLI**: `dev-team --verify DIR --finding <id-or-claim-substring>`
  (`--json`, `--budget-usd`). Runs an agent, so it sits behind the
  credential preflight — unlike `--make-backlog`.
- **Dispatch** (`docs/DISPATCH.md`): a `verify` job mode (validated
  synchronously against disk at submit time) plus
  `GET /jobs/{id}/findings` and `GET /jobs/{id}/verifications`. Assess
  runs now mirror `audit/<id>/meta.json` (repo identity) beside the
  assessment JSON, and verify verdicts append to
  `audit/<source>/verifications.jsonl` — all disk-keyed, so the whole
  flow survives a service restart.
- **Verdict calibration** (`docs/DISPATCH.md`): `GET /calibration`, a pure,
  $0, disk-only aggregate rolling up every persisted
  `audit/*/verifications.jsonl` across jobs into per-phase and overall
  `confirmed`/`refuted`/`needs_context` counts with a `confirm_rate` —
  the cross-job rollup `docs/VISION.md` names as the verdict-calibration
  gap left after per-job re-verification (PR #25) shipped. No agent calls;
  an out-of-contract verdict or non-string `finding_id` is dropped, not
  trusted, same as the write-time fail-secure posture.
- **`POST /jobs/{id}/cancel`** (`docs/DISPATCH.md`): the missing rung on
  the job lifecycle — a still-`queued` job can now be pulled out of the
  single-flight queue (`queued → cancelled`, refused with `409` once a job
  is `running` or already terminal) instead of the only prior options
  (wait for it to run anyway, or restart the whole service and lose every
  other queued job). Cancelling is $0 and strictly cheaper than letting a
  job run: the targeted job never reaches `run_job`, so no clone, no
  workspace, no agent call happens for it, and it stops counting toward
  the queue cap. `GET /jobs/{id}/result` on a cancelled job answers
  `{"kind":<mode>,"success":false,"error":"cancelled","cost_usd":0}`. No
  new auth surface, no new store — reuses the existing bearer gate and the
  in-memory registry's lock, shared with the worker's `queued → running`
  flip so the two transitions are mutually exclusive.
- **`POST /jobs/{id}/purge`** (`docs/DISPATCH.md`): permanent, **archive-
  gated** deletion — the follow-up #35 deferred as its own proposal.
  Requires the job to already be `archived` (`409` otherwise), turning
  irreversible deletion into an explicit two-step action. Removes exactly
  three things: the job's workspace clone (`jobs_root/{id}`), the
  `audit/{id}/` mirror (`assessment.md`/`assessment.json`/`meta.json`/
  `verifications.jsonl`, each removed through the traversal/symlink-escape-
  checked `Workspace.delete()` — never a raw filesystem call), and backlog
  stories bred from that job (same write lock `DELETE /backlog/story/{id}`
  uses). `events.jsonl`/transcripts are out of scope for v1. Not idempotent:
  a second purge on the same id is `404`, never a redundant `200`. The
  terminal-state check reads `record.state` directly within a single locked
  block rather than reusing `archive_job`'s internal helper (which itself
  acquires the same lock and would deadlock the single-flight dispatcher).
  The dashboard gained a "delete permanently" button on each archived run
  row, behind the same two-step confirm the backlog's story-delete already
  uses (`docs/DASHBOARD.md`).

### Dashboard
- **`dev-team --dashboard` serves a local web dashboard over the
  workspace** (`docs/DASHBOARD.md`): one card per agent with its current
  stage and last activity, a live feed, recent runs, the backlog with
  story-point progress and status chips, cross-run memory (retrospectives,
  ADRs), captured conventions, and in-place viewing of assessment reports.
  Stdlib-only (`http.server`), read-only over the workspace, self-contained
  page (no external assets), localhost by default (`--port` / `--host` to
  widen — it is unauthenticated, so only on trusted networks). A JSON API
  (`/api/state`, `/api/report`) backs the page and is usable by other
  tooling.
- **Runs journal their progress**: every `--deliver`/`--assess` run appends
  timestamped events (role, stage, message, persona, run id) to
  `.dev_team/events.jsonl` — bounded, corruption-tolerant — which is what
  the dashboard reads. Library users get the same via `EventLog` composed
  into the engine `listener`.
- **Verdict calibration panel** (`docs/DASHBOARD.md`): the dashboard now
  renders the same per-phase/overall confirmed/refuted/needs-context rollup
  `GET /calibration` computes, next to House conventions — computed
  in-process from `audit/<id>/verifications.jsonl` on the shared workspace
  tree rather than proxied to the dispatch service, so it works standalone.
  Respects the same `include_archived` exclusion as the rest of the page;
  a zero-verification workspace renders a muted empty state, not an empty
  table.

### Sources
- **`--repo owner/name` fetches the repository itself** (also full HTTPS /
  SSH / `file://` URLs): the ref is cloned into the workspace — per-repo
  directory under `./build/` by default, or exactly `--workspace` — and an
  existing clone of the same remote is fast-forwarded instead of re-cloned
  (anything else at the destination is refused, and local changes fail the
  update loudly rather than being overwritten). Valid with `--assess`,
  `--deliver`, and `--chat`.
- **The env file is configured once and found automatically**: without
  `--env-file`, the default search checks `./.env`, then
  `$XDG_CONFIG_HOME/dev-team/dev-team.env`
  (`~/.config/dev-team/dev-team.env`), then `/etc/dev-team/dev-team.env` —
  no per-run flag needed. The fetch line on stderr names the env file a run
  used, so credential debugging never involves guessing.
- **PAT auth with strict token hygiene**: `GITHUB_TOKEN`/`GH_TOKEN` is read
  from an env file (see the default search above) or, failing
  that, taken *out of* the process environment. git receives the credential
  through per-command `GIT_CONFIG_*` variables (an `http.extraheader`
  basic-auth header) — never the URL — so nothing token-shaped lands in
  argv, `.git/config`, process listings, or the environment of any command
  the engines execute (gates, build probes, delivered code). Clone errors
  are scrubbed and a 404 explains that GitHub hides unauthorized private
  repositories.
- `CommandRunner.run()` grew an optional `env` overlay for exactly this
  kind of single-command secret; `GuardedCommandRunner` forwards it only
  when set, so pre-existing custom runners keep working.

### Assessment
- **`rebuild` is a first-class classification**: the recommendation phase's
  fixed vocabulary now distinguishes an incremental `strangler-rewrite`
  from a big-bang `rebuild` (build a replacement from scratch; the old
  system is a requirements document, not a foundation), and the prompt
  defines every option so the choice is deliberate.
- **Opt-in build probe** (`--build-probe` / `AssessConfig.build_probe`):
  the detected profile's setup/verify commands are actually executed —
  exit codes and output tails feed the buildability auditor as ground
  truth and land in the report appendix and `--json` (`build_probe`).
  Commands stop at the first failure; profiles with no locally runnable
  commands (legacy .NET Framework) skip with a recorded reason. Off by
  default: it runs the repository's own build (arbitrary code) and
  mutates the working tree the way any build does.
- **Lockfile parsing for the OSV scan**: exact resolved versions from
  `package-lock.json` (v1–v3), `poetry.lock`, `Cargo.lock` (workspace
  crates skipped), and NuGet `packages.lock.json` (`Project` references
  skipped) join the manifest pins, so range-specified projects still get
  a live vulnerability scan instead of the model-knowledge fallback.
- **Audit blind spots**: the report appendix and `--json` (`blind_spots`)
  deterministically name the top-level directories no phase finding (nor
  dead-code probe) cited — a sampled audit can no longer read as a
  complete one. Evidence citations count whether agents return a string
  or a list of paths.
- **Broken citation detection**: the report appendix and `--json`
  (`broken_citations`) deterministically flag findings that cite a bare
  file path (`Web.config`, `src/Api/Program.cs:42`) not present in the
  repository — the opposite failure mode from blind spots, and the $0
  automatic counterpart to the `--verify` re-check's "a citation that
  doesn't exist is itself a result." No agent call, no filesystem read of
  the cited path — pure set-membership against the already-enumerated file
  list. Prose and multi-part citations are deliberately left unflagged to
  keep the heuristic false-positive-free.

## [0.7.0] — Legacy-repo analysis: dead code, live CVEs, conventions, remote CI

### Assessment
- **Deterministic dead-code probes** feed the audit with exact, citable
  findings (no model guessing): `.cs` files no legacy MSBuild project
  compiles (`<Compile Include>` diff vs disk), `.csproj` files no `.sln`
  references, and top-level directories dormant for `dormancy_days`
  (default 365) while the repo stayed active (git-based; skipped cleanly
  outside git). Findings land in the evidence block, the report appendix,
  and `--json` (`dead_code`).
- **Live dependency vulnerability scanning via OSV.dev**: exact pins parsed
  deterministically from `packages.config` (NuGet), `package.json`,
  `requirements.txt`, and `Cargo.toml` are checked against the OSV batch
  API in one call — every ecosystem, one endpoint. Offline or failed
  queries degrade to a labelled model-knowledge fallback; `--no-osv-scan`
  opts out. The report footer now says which mode produced the CVE claims.
- **House-conventions capture**: a parallel side-phase profiles the
  repository's own style — naming, layout, test patterns, error handling —
  with citations, merges in machine-readable configs (`.editorconfig`,
  ReSharper `.DotSettings`, linter configs, rulesets), and persists to
  `.dev_team/conventions.json` (`--no-conventions` opts out). Advisory:
  its failure degrades the report, never the audit verdict.
- **Assessment → backlog bridge** (`--backlog` / `update_backlog`):
  remediation-plan steps, must-fix build blockers and dependencies,
  hardcoded secrets, dead-code hits, and live vulnerability records become
  estimated stories under one "Assessment remediation" epic in the
  persistent backlog, deduplicated by title so re-audits refresh instead
  of flood. This is the loop from "audited" to "remediated": delivery
  runs can now work the audit off story by story.
- **Exclude globs and component fan-out for monoliths**: vendored noise
  (`packages/`, `node_modules/`, `bin/`, `obj/`, binaries) is excluded
  from the tree, stats, and component detection by default (`--exclude`
  replaces the defaults; `--max-tree-entries` raises the cap), and
  `--component-fanout` gives each detected sub-project (per-directory
  manifests, `.csproj` included) its own parallel deep-dive section in
  the report, capped by `max_components`.

### Delivery
- **Legacy .NET Framework detection**: `packages.config` anywhere, or
  old-style project XML (`ToolsVersion`/`TargetFrameworkVersion`),
  resolves to a `dotnet-framework` profile that is *not locally runnable*
  — `dotnet test` never was going to work — instead of a profile that
  fails every task.
- **Graceful verification degrade**: on a stack with no runnable local
  verify command, gates degrade to an explicit `verification-unavailable`
  marker: review, security, and static findings become the quality bar,
  the fail-to-pass check is disabled as meaningless, and the events say
  exactly what happened and how to do better.
- **Remote CI verification gate** (`--remote-verify-status` /
  `--remote-verify-trigger`): delegate the Definition of Done to the CI
  system that *can* build the repo — trigger a run, poll a status command
  until it exits zero. Any CI with a CLI plugs in; polling cadence is
  configurable (`remote_verify_max_polls`, `remote_verify_interval_seconds`).
- **Conventions-aware engineer and reviewer**: a stored conventions
  profile is injected into implementation prompts ("match the house
  style") and review prompts (deviations are findings), so work on a
  legacy repo lands in the repo's own idiom instead of a modernisation
  patchwork.

### Assessment & .NET
- **A third engine audits existing repositories** (`--assess` /
  `DevTeam.assess`): read-only by construction (no branch, no gates, no
  commits, no bookkeeping; auditors get `Read`/`Grep`/`Glob` rooted at the
  workspace), five phases across the cast — inventory (architect),
  buildability without running installs (DevOps), dependency/secret/data/
  external-service risk (security), test & doc reality (QA), and a fixed-
  vocabulary classification (revive-in-place / dependency-surgery /
  strangler-rewrite / archive) with a sequenced remediation plan and the
  single highest-risk item (product manager). Deep phases run in parallel;
  every claim carries a file-path citation; a deterministic LOC/extension
  inventory anchors the prompts; failed phases degrade into the report
  instead of unwinding the run; `--interactive` adds a post-inventory scope
  pause (narrow or abort). Output is one cited markdown report (default
  `audit/assessment.md`, the run's only write) or `--json`. See
  `docs/ASSESSMENT.md`.
- **.NET support**: root `.sln`/`.csproj`/`global.json` resolve to a
  `dotnet` profile (`dotnet test`/`restore`, vulnerable-package scan) that
  wins over `package.json` so full-stack monoliths resolve to their
  solution; repo context reads .NET manifests; baseline attribution parses
  VSTest and xUnit failure output.

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
- **`DEPLOYMENT.md` hardening from a real bare-metal install**: prerequisites
  now include `python-is-python3` (a minimal Ubuntu server ships only
  `python3`, but auto-detected verify/gate commands and agent-authored tests
  call bare `python`, so a task is wrongly reported failed without it), and the
  systemd section warns that `EnvironmentFile` — unlike a shell `source` — does
  not strip an inline `#` comment, so a `KEY=value  # note` on the credential
  line corrupts the token into a confusing `401 Invalid bearer token`.

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
