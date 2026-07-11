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
and a per-run quality scorecard. The items below are the known, deliberately
deferred capabilities, roughly in priority order.

## 1. Container-level sandboxing

**Why:** running agent-authored tests is arbitrary code execution.
`SideEffectPolicy` is defence-in-depth, not containment — and the agentic
engineer's own Bash tool is bounded only by SDK permissions and `max_turns`.

**Shape:** a `CommandRunner` implementation that executes inside a rootless
container (no credentials, no network by default, workspace bind-mounted),
so the isolation boundary matches the trust boundary; the same container
hosts the engineer's tool loop.

## 2. PR / CI integration

**Why:** a dev team's real interface is a pull request reviewed by humans and
CI, not a local commit.

**Shape:** a delivery target that pushes the `dev-team/<feature>` branch,
opens a PR (with the outcome report as the body), watches required checks,
and feeds CI failures back into the task loop as gate feedback.

## 3. Dynamic re-planning & escalation

**Why:** today a failed task just fails the run (dependants cascade-skip). A
real team re-plans: split the task, try another approach, or surface a
decision to a human.

**Shape:** on task failure, return control to the manager with the failure
evidence; allow plan mutation (replace/split tasks) within budget; route
"stuck" decisions through the `ApprovalGate` as questions, not just yes/no.

## 4. Retrieval + context budgeting

**Why:** large repos exceed any context window. The v0.5 repo map is a capped
tree + manifest heads; prompts still carry whole files (truncated). Retrieval
keeps evidence high while cost stays flat.

**Shape:** embed-and-retrieve over the repo map (symbols, not just paths);
per-role token budgets; summarised hand-offs on the blackboard instead of raw
artifacts.

## 5. Session continuity across attempts

**Why:** each engineer attempt is a fresh SDK session; on retry it re-explores
the repo from zero. Attempt N should continue attempt N-1's session with the
gate feedback appended — cheaper and smarter.

**Shape:** a session-holding `AgentRunner` built on `ClaudeSDKClient`, keyed
per task, with explicit reset on rollback.

## 6. LLM retrospectives & benchmark history

**Why:** v0.5's retrospectives are deterministic distillations, and evals run
on demand. Getting *better over time* needs richer lessons and a score trail.

**Shape:** an optional retrospective agent that mines the trace for root
causes; a standing benchmark suite run in CI against the real runner
(budget-capped, nightly) with score history so prompt/orchestration changes
show up as deltas.

## 7. MCP tool provider & group review

**Why:** specialist agents benefit from real tools (dependency scanners,
linters, issue trackers) and from debate on contentious calls.

**Shape:** expose MCP servers through `allowed_tools`; for high-severity
review disagreements, a short structured debate (reviewer vs engineer,
security as judge) before the verdict is final.
