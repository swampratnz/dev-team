# Roadmap — what it takes to be the best multi-agent dev team

v0.3 fixed the foundations (agentic engineer, evidence-based review, gates in
the workspace, merge-queue integration, graceful budgets, checkpoint/resume,
evals). v0.4 made the team behave professionally in a repo (green-baseline
requirement, dedicated delivery branch, curated commits, diff-defined review,
project-profile gate detection, ignore-aware listings, gate timeouts,
fingerprinted checkpoints). v0.5 delivered brownfield depth and scale: a repo
map feeding the planner/architect, test-level baseline attribution (tolerated
red baselines gate on *new* failures only), per-task git worktrees with
squash-merge integration, deterministic retrospectives feeding the next run,
and behavioural eval checks. v0.6 made every agent benchmark-grounded (see
docs/BENCHMARKS.md): fail-to-pass QA validation, SAST-triaging security,
budgeted evidence-based review, ADR-consistent designs with tradeoffs,
INVEST-linted plans, artifact-shipping writer/DevOps, checklist-driven SRE,
and a per-run quality scorecard. v0.7 opened the loop to humans: named
personas, interactive plan review / failure escalation / approvals over an
`InteractionChannel`, a chat front door on a persistent session, and a
`QueueChannel` integration surface for external UIs (see
`docs/INTERACTION.md`). It also added a third engine — read-only repository
assessment with cited, phased audit reports (see `docs/ASSESSMENT.md`) — and
.NET project support (profiles, manifests, VSTest/xUnit attribution). The
items below are the known, deliberately deferred capabilities, roughly in
priority order.

## 1. Container-level sandboxing *(done)*

**Why:** running agent-authored tests is arbitrary code execution.
`SideEffectPolicy` is defence-in-depth, not containment — and the agentic
engineer's own Bash tool is bounded only by SDK permissions and `max_turns`.

**Shape:** a `CommandRunner` implementation that executes inside a rootless
container (no credentials, no network by default, workspace bind-mounted),
so the isolation boundary matches the trust boundary; the same container
hosts the engineer's tool loop.

**Status:** phases (a) and (b) have landed (`dev_team.sandbox`, see
[`docs/SANDBOX.md`](SANDBOX.md)). **(a)** the primitive — `ContainerCommandRunner`
+ `SandboxConfig` — boxes every non-`git` command in a `docker`/`podman` run with
no network, dropped capabilities, no-new-privileges and resource limits, mounts
only the workspace, and forwards env via a `0600` `--env-file` (never argv), while
git porcelain self-delegates to the host. **(b)** wiring — `EngineConfig.sandbox`
/ `AssessConfig.sandbox` and the `--sandbox` CLI opt-in box the delivery gates and
the assessment build probe (no gate/git runner split needed, since git
self-delegates). **(c)** process-level — shipped as deployment guidance: the
engineer's own SDK tool loop bypasses the `CommandRunner`, so it is contained by
running the whole process in a container/VM ([`DEPLOYMENT.md` §5d](../DEPLOYMENT.md)
has a hardened recipe and the layered model), with matching hardening on the
standing systemd units. The remaining open edge is a per-job isolation boundary
(review S4) — one dispatched job's container can still see another's workspace on
a shared host; a per-job rootless container/namespace is the follow-up. A second
known-uncovered surface: visual review's served app (`SubprocessAppServer`) runs
as a bare host subprocess even under `--sandbox` — the engine now logs an
advisory warning rather than leaving the gap silently assumed-covered (see
[`docs/SANDBOX.md`](SANDBOX.md)); real containment is future work (needs
inbound-from-host connectivity with denied outbound, a shape this repo's
network-isolation primitives don't yet support).

## 2. PR / CI integration

**Why:** a dev team's real interface is a pull request reviewed by humans and
CI, not a local commit.

**Shape:** a delivery target that pushes the `dev-team/<feature>` branch,
opens a PR (with the outcome report as the body), watches required checks,
and feeds CI failures back into the task loop as gate feedback.

**Shipped (push + open):** the primitives — `GitRepo.push` (credential only in
the per-command `http.extraheader` env, `--force-with-lease` only) and a
`PullRequestPublisher` (GitHub REST, injectable transport, token-scrubbed
errors) — plus `delivery_target.publish_pull_request`, which pushes the
committed branch and opens the PR with `render_delivery_summary` as the body.
Token hygiene (`sources.git_auth_env` + `scrub_credentials`) is baked into the
push, never opt-in. Wired to the CLI as `--deliver --repo … --pull-request`
(with `--pr-base` / `--pr-draft`); the resolved token and repo ref are threaded
from the clone rather than re-resolved, and the PR URL is surfaced in the run
summary and JSON (`DeliveryOutcome.pull_request_url`).

**Watch primitive (shipped):** `dev_team.checks` — a `GitHubChecksReader`
(injectable GET transport, token only in `Authorization`, scrubbed errors, same
hygiene as the publisher) and a `watch_checks` poller (bounded like
`RemoteCIGate`, injectable `sleep`). It reads the PR head's check-runs plus the
legacy combined status and folds them into a `ChecksOutcome`
(`success`/`failure`/`pending`/`timeout` + the failing check names and a
token-free digest), classifying on what a human sees — any failed check-run (or
a failed combined status) fails the watch, and the combined status's noisy
"pending" (which never clears on Actions-only repos) is ignored.

**Watch wired (shipped):** opt-in `--watch-checks` (with `--watch-timeout`,
requires `--pull-request`). After the PR opens, the CLI polls its checks on the
delivered branch via `GitHubChecksReader`/`watch_checks` and records the
`ChecksOutcome` on `DeliveryOutcome.checks` — surfaced in the run summary and
JSON (`checks_state`/`checks_failed`) and reflected in the exit code (a failed
or timed-out watch makes the run non-zero even though the PR opened). A read
error is reported cleanly and never sinks a delivery whose PR did open.

**Remediation primitive (shipped):** `DeliveryEngine.remediate_checks(ci_failure)`
— one agentic pass to make a delivered PR's failing CI go green. The failing-
checks text is untrusted CI output (a fork workflow's logs can be attacker-
influenced), so it reaches the engineer as a `fences.defuse`'d, delimited
`<ci-output>` block (declared off-limits by the engineer's system-prompt note);
the engineer fixes the workspace in place, the Definition-of-Done gates decide,
and a fix is committed to the branch only when they pass (a fix that doesn't is
discarded, leaving the branch untouched; a passing gate with no change reports
no-fix rather than an empty commit). It never pushes or opens PRs — the caller
drives that.

**Loop closed (shipped):** opt-in `--watch-fix-rounds N` (default 0 = watch +
report only; requires `--watch-checks`). On a failed watch the CLI holds the
delivery engine and loops up to N rounds — `remediate_checks` → `push_branch`
the fix to the PR branch (`--force-with-lease`, the token hygiene shared with
the publish path) → re-watch — stopping on green, on a round that fixes
nothing, on a push failure, on budget exhaustion, or when a human skips. It runs
autonomously when unattended and asks apply/skip per round (`ci_fix_question`)
when an interaction channel is attached; a `pending`/`timeout` watch is left
alone rather than chased. With that, **ROADMAP #2 is complete**: deliver → push
→ open PR → watch required checks → fix a failure and re-push, closing the loop.

## 3. Dynamic re-planning

**Why:** interactive runs now escalate a failed task to the *human* (skip, or
retry with guidance), but unattended runs still just fail it, and the plan
itself never mutates mid-run. A real team re-plans: split the task, try
another approach.

**Shape:** on task failure, return control to the manager with the failure
evidence; allow plan mutation (replace/split tasks) within budget, using the
same question vocabulary interactive runs use so a human can supervise the
re-plan when a channel is attached.

**Shipped:** opt-in via `EngineConfig.max_replan_rounds` / `--max-replan-rounds N`
(default 0 = off). After the schedule leaves tasks failed, a bounded loop asks
the product manager (`ProductManagerAgent.replan`) for a targeted mutation of
each still-failed task — split / replace / drop — which `replan.apply_replan`
splices into the plan (dropping the task, rewiring dependents, re-linting), then
re-schedules the not-yet-attempted tasks through the same worker. A human
supervises each proposal (apply / revise / reject, via `replan_review_question`)
when an interaction channel is attached; otherwise it applies autonomously.
Bounded by the round count and the budget; the per-task retry-with-guidance
escalation (`_escalate_failure`) is unchanged and sits underneath it.

## 4. Retrieval + context budgeting

**Why:** large repos exceed any context window. The v0.5 repo map is a capped
tree + manifest heads; prompts still carry whole files (truncated). Retrieval
keeps evidence high while cost stays flat.

**Shape:** embed-and-retrieve over the repo map (symbols, not just paths);
per-role token budgets; summarised hand-offs on the blackboard instead of raw
artifacts.

**Shipped:** a *deterministic lexical* retriever (`dev_team.retrieval`,
BM25 over tokenised file content with filename/symbol up-weighting) rather than
embeddings — no provider, no network, exactly testable, matching the repo map's
stance. Opt-in via `EngineConfig.retrieval` / `--retrieval`, bounded by a
per-role token budget (`retrieval_token_budget` / `--retrieval-tokens`, via a
char≈token estimator). Wired into the architect's prompt (so it designs against
the most-relevant real code, not just the file tree) and into the described
engineer's prompt (so it writes against the body of the files it will touch,
not just their paths in the listing — the untrusted-content guard now covers
the engineer too, since retrieved code enters its prompt). The cross-run
blackboard hand-off is now *summarised* rather than counted: the persisted
memory that seeds the next run's planning prompt renders a bounded, per-kind
digest of what earlier runs produced (each artifact's own one-line summary —
what was built, what the tests cover, the security verdict), grouped and capped
with explicit "... and N more" tails, instead of the old bare artifact *count*.
Decisions and retro notes accumulate across runs as before.

## 5. Session continuity across attempts

**Why:** each engineer attempt is a fresh SDK session; on retry it re-explores
the repo from zero. Attempt N should continue attempt N-1's session with the
gate feedback appended — cheaper and smarter. (`--chat` already holds a
persistent `ClaudeSDKClient` session, so the transport pattern is proven —
it just isn't used for engineering attempts yet.)

**Shape:** a session-holding `AgentRunner` built on `ClaudeSDKClient`, keyed
per task, with explicit reset on rollback.

**Shipped:** opt-in via `EngineConfig.reuse_engineer_session` /
`--reuse-engineer-session` (default off; agentic only). `sdk.AgentSession`
/ `ClaudeAgentSession` hold a tool-enabled `ClaudeSDKClient` open across a task's
attempts (metered per turn by `instrument.InstrumentedSession`); the engineer's
first attempt sends the full prompt and each retry
(`EngineerAgent.implement_over_session`) sends only the feedback, so the model
keeps the code it read and its prior attempt instead of restarting cold. A
session turn that errors is discarded and the attempt retried once on the proven
cold path (`_engineer_attempt`); per-attempt model escalation applies only to
that cold path (the session's model is fixed). Worktree mode now composes with
session reuse too: `_develop_task_in_worktree` opens one session per task,
rooted in that task's own worktree, reusing the same `_open_engineer_session`
/ `_engineer_attempt` machinery — closed before the worktree is removed so a
live session never outlives its directory. An on-by-default flip is the
remaining follow-up.

## 6. LLM retrospectives & benchmark history

**Why:** v0.5's retrospectives are deterministic distillations, and evals run
on demand. Getting *better over time* needs richer lessons and a score trail.

**Shape:** an optional retrospective agent that mines the trace for root
causes; a standing benchmark suite run in CI against the real runner
(budget-capped, nightly) with score history so prompt/orchestration changes
show up as deltas.

**In progress:** the LLM retrospective agent has landed. Opt-in via
`EngineConfig.llm_retrospective` / `--llm-retrospective` (default off), it runs
after delivery over a compact, bounded run digest (`_run_evidence`: task
outcomes, the scorecard, the trace's shape, and the spend — never the raw
transcript, fenced as untrusted `<evidence>`) and returns a few *root-cause*
lessons that name a cause and a change, richer than the always-on deterministic
distillation it complements. It runs under the graceful specialist wrapper and
is skipped once the budget is spent, so it never gates a delivery; its lessons
merge into the persisted retrospective that seeds the next run's plan.

The deterministic **score-history** trail has also landed (`dev_team.scores`):
every delivery appends a compact `RunScore` (success, tasks, total attempts,
cost, the scorecard counters) to a bounded `.dev_team/score-history.json`, and
`ScoreHistory.render` shows each run's headline metrics with signed deltas from
the run before it (a run also logs its delta as an event) — so a prompt or
orchestration change shows up as a movement rather than a vibe. No LLM, no
network.

The standing **benchmark suite** has also landed (`dev_team.benchmark`): a
fixed set of `EvalCase`s run through the real team and scored by `evals.evaluate`
(green, security-approved, production-ready, within a per-case budget), exposed
as the `dev-team-benchmark` console entry point and wired into a nightly CI
workflow (`.github/workflows/benchmark.yml`). That workflow is **disabled by
default** — its one enable switch is the `RUN_BENCHMARKS` repository variable, so
both the nightly run and manual dispatch stay completely inert (no spend, no
credential use) until an admin sets it to `true` and provides a Claude
credential secret — the governance guard for scheduled real spend.

**Done:** the benchmark's own cross-run trend trail has landed
(`dev_team.benchmark_history`): an opt-in `--history-file PATH` flag on
`dev-team-benchmark` (unset by default — zero disk I/O, today's behaviour
unchanged) appends a bounded `BenchmarkRun` (cases total/passed, cost,
timestamp) to a local JSON trail and prints the signed pass-rate/cost delta
against the prior run. `.github/workflows/benchmark.yml` restores and saves
that file via `actions/cache` (no repo write, `permissions: contents: read`
unchanged) so the trail persists across nightly CI runs without committing
anything to git — mirroring `ScoreHistory`'s bounded-trail, fail-secure-load
shape one level up, from a single delivery to the whole suite.

## 7. Richer interaction surfaces

**Why:** the interactive core (plan review, escalation, approvals, chat)
lives on a UI-agnostic `InteractionChannel`, but the only shipped surface is
the terminal. On a headless server the natural interfaces are a web
dashboard, Slack, or the pull request itself.

**Shape:** thin adapters servicing a `QueueChannel` — a web dashboard
(events over SSE/WebSocket, questions as buttons), a Slack bot (questions as
threads), and a PR-comment loop that composes with roadmap item 2. The
engine needs no changes; each surface is an adapter plus notification
routing.

**PR-comment loop (shipped):** `dev_team.pr_comment_channel.GitHubPRCommentChannel`
— an `InteractionChannel` that posts a question as a comment on the delivered
PR and polls (bounded, injectable `sleep`, mirroring `watch_checks`) for a
reply from an **explicitly configured allow-list** of GitHub logins (no
implicit "defaults to the PR author" — an unspecified default is a security
gap, not a convenience). A reply's first whitespace-trimmed, lower-cased token
must exactly match a live question choice key; anything else (an unauthorized
commenter, an unrecognised reply) is silently skipped, and an exhausted poll
returns the question's fail-safe choice, exactly like `ConsoleChannel`'s EOF
behaviour. Wired only into the CI-fix loop (`ci_fix_question` is the only
question that fires after a PR exists) via opt-in `--interactive-pr-comments`
(requires `--interactive`, `--pull-request`, `--watch-fix-rounds > 0`, and at
least one `--interactive-pr-comment-author LOGIN`); when set it replaces just
that loop's channel — `team.interaction` (plan review, approvals) is
untouched. **Exposure change to weigh before enabling:** this posts the CI
failure summary as a plain, repo-visible PR comment, a broader audience than
the terminal or dispatch's bearer-token-gated question endpoint (#87) — see
`docs/INTERACTION.md`. The dashboard and Slack adapters remain future work.

## 8. MCP tool provider & group review

**Why:** specialist agents benefit from real tools (dependency scanners,
linters, issue trackers) and from debate on contentious calls.

**Shape:** expose MCP servers through `allowed_tools`; for high-severity
review disagreements, a short structured debate (reviewer vs engineer,
security as judge) before the verdict is final.
