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
a shared host; a per-job rootless container/namespace is the follow-up.

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

**Remaining:** watching the PR's required checks after opening it, and feeding
CI failures back into the delivery task loop as gate feedback (closing the loop
rather than stopping at "PR opened").

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

**In progress:** a *deterministic lexical* retriever (`dev_team.retrieval`,
BM25 over tokenised file content with filename/symbol up-weighting) rather than
embeddings — no provider, no network, exactly testable, matching the repo map's
stance. Opt-in via `EngineConfig.retrieval` / `--retrieval`, bounded by a
per-role token budget (`retrieval_token_budget` / `--retrieval-tokens`, via a
char≈token estimator). Wired into the architect's prompt (so it designs against
the most-relevant real code, not just the file tree) and into the described
engineer's prompt (so it writes against the body of the files it will touch,
not just their paths in the listing — the untrusted-content guard now covers
the engineer too, since retrieved code enters its prompt). Remaining: summarised
blackboard hand-offs (today the persisted hand-off is only decisions + retro
notes + a bare artifact *count*).

## 5. Session continuity across attempts

**Why:** each engineer attempt is a fresh SDK session; on retry it re-explores
the repo from zero. Attempt N should continue attempt N-1's session with the
gate feedback appended — cheaper and smarter. (`--chat` already holds a
persistent `ClaudeSDKClient` session, so the transport pattern is proven —
it just isn't used for engineering attempts yet.)

**Shape:** a session-holding `AgentRunner` built on `ClaudeSDKClient`, keyed
per task, with explicit reset on rollback.

**Shipped:** opt-in via `EngineConfig.reuse_engineer_session` /
`--reuse-engineer-session` (default off; agentic, non-worktree only). `sdk.AgentSession`
/ `ClaudeAgentSession` hold a tool-enabled `ClaudeSDKClient` open across a task's
attempts (metered per turn by `instrument.InstrumentedSession`); the engineer's
first attempt sends the full prompt and each retry
(`EngineerAgent.implement_over_session`) sends only the feedback, so the model
keeps the code it read and its prior attempt instead of restarting cold. A
session turn that errors is discarded and the attempt retried once on the proven
cold path (`_engineer_attempt`); per-attempt model escalation applies only to
that cold path (the session's model is fixed). Worktree mode and an on-by-default
flip are the remaining follow-ups.

## 6. LLM retrospectives & benchmark history

**Why:** v0.5's retrospectives are deterministic distillations, and evals run
on demand. Getting *better over time* needs richer lessons and a score trail.

**Shape:** an optional retrospective agent that mines the trace for root
causes; a standing benchmark suite run in CI against the real runner
(budget-capped, nightly) with score history so prompt/orchestration changes
show up as deltas.

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

## 8. MCP tool provider & group review

**Why:** specialist agents benefit from real tools (dependency scanners,
linters, issue trackers) and from debate on contentious calls.

**Shape:** expose MCP servers through `allowed_tools`; for high-severity
review disagreements, a short structured debate (reviewer vs engineer,
security as judge) before the verdict is final.
