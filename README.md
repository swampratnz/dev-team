# dev-team

A **multi-agent software development team** built on the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

`dev-team` coordinates a roster of role-specialised AI agents — the same roles a
real engineering team has — and drives a feature request through the entire
software development lifecycle: planning, design, implementation, code review,
QA, security, docs, reliability, and deployment.

- ✅ **Every role a development team needs**, modelled as an agent.
- ✅ **A real agent loop** — in agentic mode the engineer gets tools
  (read/write/edit/run) and works *inside* the workspace: it reads existing
  code before changing it, writes tests, and runs them.
- ✅ **Evidence-based review** — the reviewer, security engineer, and QA are
  shown the actual content of changed files; pass/fail comes from real exit
  codes, never from self-report.
- ✅ **Real, gated execution** — gates and git run in the *workspace root*;
  integration is serialised like a merge queue; failed attempts are rolled
  back, while each *accepted* task is banked as a `wip(dev-team)` commit on
  the delivery branch (so a later rollback can never destroy gated work);
  the curated feature commit happens only after security approves.
- ✅ **Behaves like a professional in your repo** — refuses to start on a red
  baseline (inherited breakage never gets blamed on the engineer) or over
  uncommitted work, does everything on a dedicated `dev-team/<feature>`
  branch, authors (or extends) a `.gitignore`, and stages a curated change
  set (never `git add -A` into the feature commit).
- ✅ **Works on legacy suites** — a tolerated red baseline records the failing
  test identities and gates each task only on *newly* failing tests
  (pytest/go/cargo/VSTest/xUnit output attribution).
- ✅ **Knows the codebase** — a deterministic repo map (tree, manifest heads,
  test layout) feeds the planner and architect on brownfield runs, and a
  retrospective of what failed last time feeds the next run's plan.
- ✅ **True parallelism (opt-in)** — `worktrees=True` gives every task its own
  git worktree: implementation *and* gate runs proceed concurrently; tasks
  squash-merge into the delivery branch one at a time with a full gate check
  on the merged state.
- ✅ **Adapts to the project** — the verify command is auto-detected from the
  workspace's manifests (dotnet / npm / cargo / go / pytest), with optional
  `setup_command` provisioning and per-gate timeouts.
- ✅ **Benchmark-grounded agents** (see [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md)) —
  QA is held to SWT-bench's fail-to-pass bar (a suite that still passes with
  the implementation reverted is rejected as vacuous); the reviewer works to a
  comment budget and triages linter output; security triages a SAST scan and
  must cite evidence for blocking findings; the architect weighs alternatives
  against prior ADRs; plans are INVEST-linted with a revision pass; the
  writer and DevOps ship real files (docs, Dockerfile, CI) in the feature
  commit; the SRE runs a launch-checklist review over the delivered code. A
  per-run scorecard tracks rejections and lint issues so agent quality is a
  measured trend.
- ✅ **Governed and resumable** — cost budgets stop the run gracefully (a
  stop-line: in-flight calls complete, so concurrent agents can overshoot by
  the cost of the calls in flight), checkpoints let a later run resume where
  a crashed or over-budget run stopped — reusing the checkpointed plan and
  squashing the banked work into the eventual feature commit — and every
  agent call is traced and metered.
- ✅ **Measurable** — an eval harness (`dev_team.evals`) scores the team on
  fixed benchmark cases so prompt/orchestration changes are judged by numbers.
- ✅ **100% test coverage** (branch coverage) of the orchestration code,
  enforced in `pyproject.toml`. Note what this does and doesn't claim: the
  suite proves the machinery with test doubles; it does not exercise the real
  Claude CLI (see *Testing*).
- ✅ **Interactive, when you want it** — `--interactive` pauses at the moments
  a human wants a say (plan review with revise/abort, failed-task escalation
  with retry guidance, commit/risky-command approval); `--chat` opens a
  conversation with the product manager to shape the request before any run
  starts; every agent has a configurable name/persona; and a `QueueChannel`
  lets you drive runs from your own UI (see
  [`docs/INTERACTION.md`](docs/INTERACTION.md)).
- ✅ **Audits what it didn't build** — `--assess` turns the team loose on an
  existing repo (legacy .NET monolith included: solution-aware profiles,
  VSTest/xUnit failure parsing) and produces a phased, path-cited assessment
  — buildability, dependency/secret/data risk, test reality, and a verdict
  from a fixed vocabulary (revive-in-place, dependency-surgery,
  strangler-rewrite, rebuild, archive) with a sequenced
  remediation plan — without mutating the repo. Opt-in `--build-probe` runs
  the project's own setup/verify commands so buildability rests on real exit
  codes, and the report names its **audit blind spots** (top-level
  directories no finding cited) so a sampled audit can't read as a complete
  one (see [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md)).
- ✅ **Finds dead code deterministically** — exact probes, no model guessing:
  sources no legacy MSBuild project compiles, projects no solution includes,
  and directories dormant for a year while the repo stayed active. Vendored
  noise is excluded by default (`--exclude` to customise) and
  `--component-fanout` deep-dives each sub-project in parallel.
- ✅ **Scans dependencies live** — exact pins parsed from the manifests
  (`packages.config`, `package.json`, `requirements.txt`, `Cargo.toml`) and
  the lockfiles (`package-lock.json`, `poetry.lock`, `Cargo.lock`, NuGet
  `packages.lock.json`) are checked against OSV.dev in one batch call
  (graceful offline fallback, `--no-osv-scan` to opt out), so CVE findings
  cite advisories, not recollections — and range-specified projects still
  get their resolved versions scanned. Detected Node.js/Python/.NET runtime
  versions get the same treatment against endoflife.date
  (`--no-eol-scan` to opt out), so EOL/support-status findings for those
  three runtimes are live too, not just training-data recollections.
- ✅ **Learns the house style and follows it** — assessment captures a cited
  conventions profile (naming, layout, test patterns, plus `.editorconfig` /
  ReSharper `.DotSettings` / linter configs), persists it, and every later
  delivery injects it into the engineer's and reviewer's prompts.
- ✅ **Closes the audit → fix loop** — `--backlog` converts findings
  (remediation steps, build blockers, must-fix dependencies, secrets, dead
  code, live CVEs) into estimated stories in the persistent backlog for
  delivery runs to work off.
- ✅ **Verifies through your CI when it must** — stacks that cannot build
  locally (legacy .NET Framework) degrade to evidence-based review instead
  of failing every task, or gate on your real pipeline via
  `--remote-verify-trigger` / `--remote-verify-status`.
- ✅ **Fetches the repo itself** — `--repo owner/name` clones (or
  fast-forwards) the target straight from GitHub and uses the clone as the
  workspace; private repositories authenticate with a `GITHUB_TOKEN` from an
  env file configured once and found automatically (`./.env`, then
  `~/.config/dev-team/dev-team.env`, then `/etc/dev-team/dev-team.env`;
  `--env-file` overrides). The token never touches the URL, argv,
  `.git/config`, or the environment of any command the agents run — it is
  handed to git per-command and stripped from the process environment.
- ✅ **Authenticates as a GitHub App** — configure `GITHUB_APP_ID` +
  `GITHUB_APP_PRIVATE_KEY_FILE` (the `github` extra) and every clone, push,
  PR, and checks read mints a one-hour installation token scoped to the
  single repository it touches, re-minted near expiry, instead of one
  long-lived PAT for everything. The dispatch service can additionally let
  users **sign in with GitHub** (OAuth) and submit jobs only against repos
  their App installations cover, run jobs on distinct repos concurrently
  (`--max-concurrent-jobs`, same-repo jobs always serialise), and report
  any repo's live CI state (`GET /checks`). See `docs/GITHUB_APP.md`.
- ✅ **Watchable** — `dev-team --dashboard` serves a local web dashboard over
  the workspace: one card per agent (current stage, what it last worked on),
  a live activity feed, recent runs, the backlog with story-point progress,
  cross-run memory, and the assessment reports. Runs journal their events to
  `.dev_team/events.jsonl`, so the dashboard is a separate read-only process
  you leave open (see [`docs/DASHBOARD.md`](docs/DASHBOARD.md)).
- ✅ **Ubuntu-ready** — packaged for deployment as a container or systemd unit.
- ✅ **Security posture, mapped** — every threat area (prompt injection,
  credential hygiene, execution/workspace containment, HTTP surface auth,
  pipeline guardrails, and what's explicitly *not* covered yet) is indexed to
  the exact module/function that implements it in one reference (see
  [docs/SECURITY.md](docs/SECURITY.md)).

The capability set was chosen from a structured research pass across seven
dimensions (roles, orchestration, execution, quality gates, memory, governance,
delivery), grounded in real multi-agent frameworks — see
[`docs/RESEARCH.md`](docs/RESEARCH.md). [`docs/ROADMAP.md`](docs/ROADMAP.md)
tracks how each release built on it and what is still deliberately out;
[`docs/INTERACTION.md`](docs/INTERACTION.md) covers working *with* the team
interactively.

---

## First 10 minutes

The fastest way in is to point the team at a repo you already have and read
what it says back — an assessment is read-only, so nothing you run here can
change your code.

1. **Install** (a venv keeps it off your system Python):

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -e '.[test]'
   ```

   The agents drive the Claude Code CLI at runtime — install it too
   (`npm install -g @anthropic-ai/claude-code`); see
   [DEPLOYMENT.md](DEPLOYMENT.md#1-prerequisites).

2. **Authenticate.** Set one credential in the environment — a Claude
   subscription token (from `claude setup-token`) or a Claude API key:

   ```bash
   export CLAUDE_CODE_OAUTH_TOKEN=...   # from: claude setup-token
   # ...or: export ANTHROPIC_API_KEY=sk-ant-...
   ```

   The CLI fails fast with guidance if neither is present.

3. **Assess a small repo.** Title and description scope the audit; the budget
   is a graceful stop-line:

   ```bash
   dev-team --assess --workspace ./some-repo \
       "First look" "what shape is this codebase in?" --budget-usd 5
   ```

   It writes a cited markdown report to `./some-repo/audit/assessment.md`
   (`--report` to change the path; `--json` for the structured shape) and
   never touches the code under audit.

4. **Watch it (optional).** In another terminal, open the read-only dashboard
   over the same workspace:

   ```bash
   dev-team --dashboard --workspace ./some-repo    # http://127.0.0.1:8737/
   ```

See [`docs/examples/assessment-sample.md`](docs/examples/assessment-sample.md)
for a full, illustrative sample report before you spend a cent, and
[`docs/ASSESSMENT.md`](docs/ASSESSMENT.md) for everything `--assess` can do.
Ready to have the team *change* code instead of just reading it? That is
`--deliver` — see *Usage* below.

---

## Three engines

| Engine | Entry point | What it does |
|--------|-------------|--------------|
| **Simulation** | `DevTeam.develop` / `DevelopmentWorkflow` | Fast, side-effect-free walk through the lifecycle — agents *describe* the work as structured data. Its reports (including QA's pass/fail) are self-described, nothing executes, and only six of the nine roles run — security, SRE, and docs are delivery-engine stages. |
| **Assessment** | `DevTeam.assess` / `AssessmentEngine` | Audits an *existing* repository read-only — inventory, buildability, risk, tests/docs, and a classification with a remediation plan — into one cited markdown report. No branch, no gates, no commits; see [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md). |
| **Real delivery** | `DevTeam.deliver` / `DeliveryEngine` | *Does* the work: the engineer works agentically in the workspace (or describes changes that are materialised for it), QA authors executable tests, gates run via a `CommandRunner` rooted at the workspace, independent tasks are implemented concurrently (described mode, or agentic with `worktrees=True`; single-workspace agentic attempts serialise) and integrated serially, and budget, tracing, memory, checkpoints, approvals, and specialist review thread through the run. Accepted tasks are banked as WIP commits on the delivery branch; the single curated feature commit happens after security approval. |

The delivery engine picks its mode from the workspace: a `LocalWorkspace`
(real directory) enables **agentic mode** — the engineer reads and edits files
and runs commands in that directory via SDK tools. An `InMemoryWorkspace` runs
**described mode** with an honest `DryRunCommandRunner` (its gate output says
`dry-run: ... not executed` rather than pretending to verify). Override with
`EngineConfig(agentic=...)`.

## The team

| Agent | Persona | Role | Responsibility |
|-------|---------|------|----------------|
| `ProductManagerAgent` | Priya | Product / delivery | Decompose a request into ordered, acceptance-criteria-bearing tasks. |
| `ArchitectAgent` | Anders | Architecture | Produce a technical design: components, tech stack, risks. |
| `EngineerAgent` | Sam | Engineering | Implement each task, and address review feedback on retries. |
| `ReviewerAgent` | Rey | Code review | Approve work or request changes with severities. |
| `QAAgent` | Quinn | Quality assurance | Design tests and report pass/fail plus coverage. |
| `SecurityEngineerAgent` | Sasha | AppSec | Threat-model and security-review the change; block on major/critical findings. |
| `TechnicalWriterAgent` | Wren | Docs | Produce user docs, API notes, and release notes. |
| `SREAgent` | Riley | Reliability | Assess production readiness: SLOs, runbook, rollback. |
| `DevOpsAgent` | Devon | DevOps | Produce a deployment plan with steps and rollback, targeting Ubuntu. |

Personas are configurable (`--roster roster.json`) or removable
(`--no-personas`); internally everything stays keyed by role — see
[`docs/INTERACTION.md`](docs/INTERACTION.md).

The simulation engine is the `DevelopmentWorkflow` state machine; the real
engine is the `DeliveryEngine`. Both are wrapped by the `DevTeam` facade.

## Capabilities

Beyond the agents, the real engine composes a set of production-shaped,
individually-testable building blocks — each a small protocol with a real and a
fake implementation:

- **Execution** — `Workspace` (in-memory / local) and `CommandRunner`
  (subprocess / dry-run / fake); `ChangeApplier` writes described changes for
  real; in agentic mode the engineer writes files itself via SDK tools.
- **Quality gates** — `Gate` / `DefinitionOfDone` run the project's verify
  command (plus any gates you compose — `CommandGate`, `CoverageGate`) from
  *actual* exit codes in the *workspace root*, driving a self-repair loop
  until green; a configured `lint_command` / `security_scan_command` is run
  and its output triaged by the reviewer / security agent. QA authors the
  executable tests the gates run.
- **Orchestration** — a dependency-aware, concurrent `schedule`; parallel
  implementation with serialised, rollback-on-failure integration (a merge
  queue). Per-role model routing and a stronger `escalation_model` for a
  task's final attempt.
- **Memory** — a shared `Blackboard`, `DecisionRecord` (ADR) log, cross-run
  `ProjectMemory` (fed back into planning as a bounded, per-kind digest of what
  earlier runs *built* — not just a count — alongside prior decisions and
  retrospective notes), a `ScoreHistory` trail (per-run metrics with
  run-over-run deltas), and a `CheckpointStore` for crash/budget-safe resume.
- **Governance** — `Budget` (graceful cost circuit-breaker), `Tracer` (audit
  spans), `ApprovalGate` (human-in-the-loop), and `SideEffectPolicy`
  guardrails. Policy is defence-in-depth, **not** a sandbox — running
  agent-authored tests is arbitrary code execution, so isolate untrusted runs
  in a container/VM.
- **Delivery** — a persistent `Backlog` (epics/stories) the engine records
  every run into, capacity-based iteration planning, `GitRepo` for
  branch/commit — one commit per delivery, after security approval.
- **Evals** — `EvalCase` / `evaluate` score deliveries against fixed
  expectations (success, expected files, cost) so quality is a tracked number.
- **Interactivity** — an `InteractionChannel` (console / queue-serviced /
  scripted / auto) pauses a run for plan review, failed-task escalation, and
  approvals; `Persona` / `Roster` give every agent a configurable name; a
  persistent `ChatBackend` powers `--chat`. Defaults preserve fully
  autonomous behaviour.

## Lifecycle

```
FeatureRequest
     │
     ▼
 ProductManager ──▶ Plan (tasks, dependencies)
     │
     ▼
   Architect  ──▶ Design
     │
     ▼
 for each task (implemented concurrently, integrated serially):
     ┌─────────────────────────────────────────────────────────┐
     │  Engineer ─▶ implements (agentic: in the workspace)      │
     │  Reviewer ─▶ reviews the actual code ── rejected ─┐      │
     │       │ approved                                  │      │
     │       ▼                                           │retry │
     │  QA ─▶ authors executable tests                   │ +    │
     │       ▼                                           │roll- │
     │  Gates ─▶ run tests/lint/... for real ── fail ────┘back  │
     │       │ green                                            │
     │       ▼                                                  │
     │  Task DONE (banked as a WIP commit + checkpointed)       │
     └─────────────────────────────────────────────────────────┘
     │
     ▼
 Security ─▶ reviews the aggregate diff  ──▶ gate for the commit
     │
     ▼
 TechWriter / SRE / DevOps ─▶ docs, readiness, deployment plan
     │
     ▼
 git commit (WIP commits squashed once, only if security approved) ─▶ DeliveryOutcome
```

A task is retried up to `max_task_attempts` times whenever review or the gates
reject it; the failed attempt is rolled back and the engineer receives the
feedback (including real gate output) on the next attempt — optionally on a
stronger `escalation_model` for the final try. If it never passes, the task is
marked `FAILED`, dependants cascade-skip, and the run reports incomplete. A
blown budget or crash leaves a checkpoint (and the accepted work banked as WIP
commits); re-running the same feature reuses the checkpointed plan, skips the
tasks already done, and squashes old and new work into one feature commit from
the original baseline.

## Architecture

The one and only integration boundary with the Claude Agent SDK is the
`dev_team.sdk` module: `ClaudeAgentRunner` implements the tiny `AgentRunner`
protocol (`prompt`, `system_prompt`, `allowed_tools`, `model`, `cwd`), and
`ClaudeChatBackend` holds the persistent conversation behind `--chat`.
`allowed_tools` + `cwd` are what turn a call into a real agent loop — the
agentic engineer passes both. Everything above the protocol is testable to
100% coverage without spawning the Claude CLI or making network calls — tests
inject a `ScriptedRunner`. Malformed agent responses are retried once with a
corrective instruction (`EngineConfig.json_retries`) before failing the stage.

```
cli ─▶ team.DevTeam ─▶ workflow.DevelopmentWorkflow ─▶ agents/* ─▶ sdk.AgentRunner
  │                                                                  ├─ ClaudeAgentRunner (real SDK)
  │                                                                  └─ ScriptedRunner (tests)
  └─ --chat ─▶ chat.ChatSession ─▶ sdk.ChatBackend (persistent session)
```

Key modules:

- `models.py` — dataclasses/enums for the whole SDLC (`Task`, `Plan`, `Design`,
  `Implementation`, `Review`, `TestReport`, `DeploymentPlan`, `ProjectResult`).
- `sdk.py` — the Agent SDK adapter and `AgentRunner` protocol.
- `agents/` — one module per role.
- `workflow.py` — the simulation state machine; `engine.py` — the real
  delivery engine (merge-queue integration, rollback, checkpointing, commit
  gating).
- `execution.py` / `verification.py` / `changes.py` / `git.py` — workspaces,
  command runners, executable gates, change application, git porcelain.
- `profile.py` / `context.py` / `failures.py` — project-type detection (the
  auto-detected verify/setup/scan commands, including legacy .NET), the
  deterministic repo map fed to planners, and red-baseline test-failure
  attribution (pytest/go/cargo/VSTest/xUnit).
- `sources.py` — `--repo`: GitHub refs resolved and cloned into the
  workspace, PAT-authenticated from an env file with strict token hygiene.
- `eventlog.py` / `dashboard.py` — the per-workspace event journal and the
  read-only web dashboard served over it (see `docs/DASHBOARD.md`).
- `dispatch.py` — an authenticated (bearer-token), single-flight HTTP service
  to submit/poll/fetch assess & deliver jobs remotely (see `docs/DISPATCH.md`).
- `transcripts.py` — opt-in capture of each agent call's raw
  system-prompt/prompt/response, viewable per-agent in the dashboard
  (off by default; see `docs/TRANSCRIPTS.md`).
- `memory.py` / `backlog.py` — blackboard, ADRs, cross-run memory,
  checkpoints, persistent backlog.
- `budget.py` / `trace.py` / `approval.py` / `policy.py` / `instrument.py` —
  governance; `InstrumentedRunner` meters and traces every agent call.
- `interaction.py` / `persona.py` / `chat.py` — human-in-the-loop questions
  and approvals, the named-agent roster, and the conversational front door.
- `scheduler.py` / `ordering.py` — dependency-aware concurrency and ordering.
- `json_utils.py` / `parsing.py` — robust extraction of structured data from
  model output (with contract enforcement: blocking findings force rejection).
- `assessment.py` — the read-only audit engine (see `docs/ASSESSMENT.md`),
  with its deterministic companions: `deadcode.py` (exact dead-code probes),
  `depscan.py` (live OSV.dev dependency scanning), and `conventions.py`
  (house-style capture and injection into later deliveries).
- `evals.py` — the benchmark harness.
- `events.py` / `report.py` / `errors.py` — progress events, result
  rendering, and the exception hierarchy.
- `team.py` — the `DevTeam` facade; `cli.py` — the `dev-team` command.

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
```

The Claude Agent SDK requires the Claude Code CLI to be available at runtime;
see the [SDK docs](https://github.com/anthropics/claude-agent-sdk-python).

Credentials: set `CLAUDE_CODE_OAUTH_TOKEN` (a Claude subscription token from
`claude setup-token`) or `ANTHROPIC_API_KEY` (a Claude API key). The CLI
fails fast with guidance when neither — nor a stored `claude` login — is
found. See [DEPLOYMENT.md](DEPLOYMENT.md#2-authentication) for details.

## Usage

### Command line

Simulation (side-effect free):

```bash
dev-team "Password reset" "Let users reset their password via an emailed link" \
    --constraint "must expire links after 1 hour" \
    --verbose
```

Real delivery — builds in a workspace directory, runs the gates, commits to a
`dev-team/<feature>` branch:

```bash
dev-team "Health endpoint" "Add a /health endpoint returning 200" \
    --deliver --workspace ./build \
    --budget-usd 5.0
```

The verify command is auto-detected from the workspace (override with
`--verify-command "npm test"`). On an existing repo the run halts up front —
before any agent spend — if the working tree is dirty
(`--allow-dirty-baseline` to override) or the quality gates are already red
(`--proceed-on-red-baseline` to override). Other `--deliver` flags:
`--setup-command "npm install"`, `--branch NAME`, `--max-concurrency N`,
`--no-commit`, `--json`.

When a task exhausts its attempts, `--max-replan-rounds N` (default 0, off) lets
the product manager re-plan around it — split it into smaller tasks, replace it
with a different approach, or drop it — and re-runs the mutated plan, bounded by
`N` rounds and the budget. Interactive runs supervise each proposal (apply /
revise / reject); unattended runs apply it autonomously.

`--reuse-engineer-session` (default off, agentic runs only) holds one SDK session
open across a task's engineer attempts, so a retry continues the prior
conversation — the code it read, the changes it made — and sends only the
feedback rather than re-establishing everything from cold. It's the biggest
token saving on retried tasks; a session that errors falls back to a cold
attempt.

`--retrieval` (default off) puts the most *relevant* existing code in front of
the architect and the described engineer, not just the repo's file tree: a
deterministic lexical ranker (BM25 over file content, filenames and symbols
weighted up) selects the top files — for the feature when designing, for the
task when implementing — and injects bounded excerpts, capped by
`--retrieval-tokens N` (per role). No embedding provider or network call — it's
exact and free, like the repo map.

`--llm-retrospective` (default off) runs a retrospective agent after delivery
that reads a compact digest of the run — each task's outcome, the scorecard, the
trace's shape, the spend — and distils a few *root-cause* lessons (naming a
cause and a change), richer than the always-on deterministic retrospective it
complements. It never gates the run (it runs under the graceful specialist
wrapper and is skipped once the budget is spent), and its lessons merge into the
cross-run memory that seeds the next run's plan.

Deliver all the way to a **pull request** — clone the repo, do the work, then
push the `dev-team/<feature>` branch and open a PR whose body is the run
summary:

```bash
dev-team "Health endpoint" "Add a /health endpoint returning 200" \
    --deliver --repo acme/api --pull-request
```

`--pull-request` requires `--repo` (so the GitHub repo, remote, and token are
known) and a commit; add `--pr-base BRANCH` to target a base other than `main`
and `--pr-draft` to open it as a draft. The token used to clone pushes and
opens the PR — it rides only in git's per-command auth header and the API
`Authorization` header, never in argv, `.git/config`, or logs — and the PR URL
is printed and included in the run summary/JSON.

Output as JSON for scripting:

```bash
dev-team "Health endpoint" "Add a /health endpoint" --json
```

Collaborate with the run instead of watching it (plan review, failure
escalation, approvals), or start from a conversation:

```bash
dev-team "Password reset" "Reset via emailed link" --interactive
dev-team --chat            # shape the request with the PM, then /run or /deliver
```

Agent names come from the default cast; rename them with `--roster FILE` or
disable with `--no-personas`. [`docs/INTERACTION.md`](docs/INTERACTION.md)
walks through all of it, including driving runs from your own UI via
`QueueChannel`.

Audit an existing repository (read-only; writes only the report):

```bash
dev-team --assess --workspace /path/to/legacy-repo \
    --report audit/assessment.md \
    "Legacy monolith" "dormant 2-3 years, frontend + backend" --budget-usd 10

# no local checkout? point at GitHub and let it clone. Configure the token
# once — it is found automatically on every later run (./.env, then
# ~/.config/dev-team/dev-team.env, then /etc/dev-team/dev-team.env) and
# never reaches the commands the agents execute
mkdir -p ~/.config/dev-team
echo 'GITHUB_TOKEN=github_pat_...' > ~/.config/dev-team/dev-team.env
chmod 600 ~/.config/dev-team/dev-team.env
dev-team --assess --repo acme/legacy-monolith \
    "Legacy monolith" "assess upgrade paths vs rebuild"

# a deep audit of a monolith: per-component fan-out, findings into the
# backlog for later delivery runs, extra excludes for vendored noise
dev-team --assess --workspace /path/to/legacy-repo \
    --component-fanout --backlog --exclude 'Libraries/*' --exclude '*/bin/*' \
    "Full audit" "dead code, upgrade candidates, house conventions"

# ground the buildability verdict in real exit codes (runs the repo's own
# build — trusted repos or a sandbox only)
dev-team --assess --workspace /path/to/legacy-repo --build-probe \
    "Buildability" "can this actually restore and test today?"

# deliver against a repo whose build only runs in remote CI
dev-team "Fix the SVG endpoint" "..." --deliver --workspace /path/to/repo \
    --remote-verify-trigger "az pipelines run --name Build" \
    --remote-verify-status "az pipelines runs show --id 123 --query succeeded"
```

Watch the team from a browser — a separate, read-only process over the same
workspace ([`docs/DASHBOARD.md`](docs/DASHBOARD.md)):

```bash
dev-team --dashboard --workspace ./build     # http://127.0.0.1:8737/
```

Set `DEV_TEAM_DASHBOARD_TOKEN` to put every dashboard route behind a token
(browser login cookie or `Authorization: Bearer`) — do this whenever the bind
is non-local or transcripts are enabled (see `docs/DASHBOARD.md`).

Exit codes: `0` success, `1` completed with failed tasks, `2` invalid input
(including an interactive abort at plan review).

### Library

```python
import asyncio
from dev_team import DevTeam, TeamConfig

async def main():
    team = DevTeam(config=TeamConfig(max_task_attempts=3))
    result = await team.develop_feature(
        "Password reset",
        "Let users reset their password via an emailed link",
        constraints=["links expire after 1 hour"],
    )
    print("success:", result.success)
    for tr in result.task_results:
        print(tr.task.id, tr.task.status.value)

asyncio.run(main())
```

### Real delivery

The delivery engine actually writes files and runs gates. Point it at a
`LocalWorkspace`; commands and git are automatically rooted at that directory
(never at your orchestrator's own cwd), and the engineer works agentically
inside it:

```python
import asyncio
from dev_team import (
    DevTeam, EngineConfig, FeatureRequest, LocalWorkspace, Budget, Tracer,
)

async def main():
    team = DevTeam()  # real Claude Agent SDK runner
    budget = Budget(limit_usd=5.0)          # graceful cost circuit-breaker
    outcome = await team.deliver(
        FeatureRequest("Health endpoint", "Add a /health endpoint returning 200"),
        workspace=LocalWorkspace("./build"),
        budget=budget,
        tracer=Tracer(),
        config=EngineConfig(
            verify_command=("pytest", "-q"),
            max_concurrency=4,
            role_models={"technical-writer": "claude-haiku-4-5-20251001"},
            escalation_model=None,  # e.g. a stronger model for final attempts
        ),
    )
    print("success:", outcome.success, "cost: $%.4f" % outcome.cost_usd)
    print("files:", outcome.workspace_files)
    print("security approved:", outcome.security.approved)
    print("committed:", outcome.committed, "resumed:", outcome.resumed_task_ids)
    print(outcome.tracer.render())

asyncio.run(main())
```

An `InMemoryWorkspace` (the default) gives a dry run: described changes, a
`DryRunCommandRunner`, and no commits.

### Evals

```python
import asyncio
from dev_team import DevTeam, EvalCase, FeatureRequest, LocalWorkspace, evaluate

team = DevTeam()
cases = [
    EvalCase(
        name="health-endpoint",
        request=FeatureRequest("Health endpoint", "Add /health returning 200"),
        expected_files=["src/app.py", "tests/test_health.py"],
        max_cost_usd=2.0,   # overspending fails the case
    ),
]

def make_engine(case: EvalCase):
    return team.make_engine(workspace=LocalWorkspace(f"./evals/{case.name}"))

report = asyncio.run(evaluate(make_engine, cases))
print(report.render())   # Evals: 1/1 passed (100%), total cost $0.8412
```

Behavioural `check_commands` only count when they really execute: on a
dry-run (in-memory) workspace they are scored as failures rather than
silently passing.

Run the eval suite before and after changing prompts, roles, or orchestration —
if the pass rate or cost regresses, so did the team.

### Safety

The delivery engine executes agent-authored code (that is what running the
tests means). `SideEffectPolicy` and `ApprovalGate` are defence-in-depth, not
containment: they guard the commands the *engine* runs (gates, git, scans, the
final commit), while the agentic engineer's own tool use is governed only by
the SDK permission layer — `acceptEdits` with a per-call `allowed_tools`
allowlist (write/run tools for the engineer, read-only `Read`/`Grep`/`Glob`
rooted at the workspace for the reviewing roles, none for text-only roles);
`bypassPermissions` is opt-in. Workspace file content is fenced as
data-not-instructions in prompts, which mitigates but does not eliminate
prompt injection from the code under review. For unattended or untrusted
runs, put the whole process in a sandboxed container/VM with no credentials
and restricted network.

### Bring your own runner

Any object implementing `AgentRunner.run(...)` can back the team — handy for
tests, dry runs, or routing to a different transport:

```python
from dev_team import DevTeam
from dev_team.testing import ScriptedRunner, json_response

runner = ScriptedRunner(by_system_prompt={
    "product manager": json_response({"summary": "...", "tasks": [...]}),
    # ...one entry per role...
})
team = DevTeam(runner)
```

## Testing

```bash
pytest
```

`pytest` is preconfigured (in `pyproject.toml`) to run with branch coverage and
**fail under 100%**. The suite uses in-memory test doubles, so it is fast and
hermetic — which also means it verifies the *orchestration*, not the models:
no test spawns the Claude CLI. Treat the eval harness (above), run against the
real runner, as the end-to-end quality signal; the unit suite is the
correctness signal for the machinery.

## Deployment on Ubuntu

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for running `dev-team` on an Ubuntu host,
either as a container or a systemd unit.

## License

MIT
