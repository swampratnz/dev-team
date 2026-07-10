# Roadmap — what it takes to be the best multi-agent dev team

v0.3 fixed the foundations (agentic engineer, evidence-based review, gates in
the workspace, merge-queue integration, graceful budgets, checkpoint/resume,
evals). The items below are the known, deliberately deferred capabilities,
roughly in priority order. Each entry says why it matters and the intended
shape, so a future slice can start from a design rather than a blank page.

## 1. Operate on an existing repository

**Why:** most dev work is brownfield. Agentic mode already works *in* a
directory; what's missing is repo onboarding: clone/branch, build a repo map
(files, symbols, conventions), and feed the relevant slice to each agent.

**Shape:** a `RepoContext` builder (tree + key file summaries, cached in
`ProjectMemory`), used by the manager (planning against reality), the
engineer (which files to read first), and the reviewer (house style).

## 2. Per-task git worktrees

**Why:** integration is currently serialised under a lock — correct, but
tasks contend for one working tree. Worktrees give true parallel
implementation *and* parallel gate runs, with merge-on-green.

**Shape:** `WorktreeManager` creating `git worktree` per task; the DoD runs in
the task's worktree; a merge step (rebase + rerun gates) replaces the lock.
Conflict → feedback to the engineer like any other gate failure.

## 3. Container-level sandboxing

**Why:** running agent-authored tests is arbitrary code execution.
`SideEffectPolicy` is defence-in-depth, not containment.

**Shape:** a `CommandRunner` implementation that executes inside a rootless
container (no credentials, no network by default, workspace bind-mounted),
so the isolation boundary matches the trust boundary.

## 4. PR / CI integration

**Why:** a dev team's real interface is a pull request reviewed by humans and
CI, not a local commit.

**Shape:** a delivery target that pushes a branch, opens a PR (with the
outcome report as the body), watches required checks, and feeds CI failures
back into the task loop as gate feedback.

## 5. Dynamic re-planning & escalation

**Why:** today a failed task just fails the run. A real team re-plans: split
the task, try another approach, or surface a decision to a human.

**Shape:** on task failure, return control to the manager with the failure
evidence; allow plan mutation (replace/split tasks) within budget; route
"stuck" decisions through the `ApprovalGate` as questions, not just yes/no.

## 6. Retrieval + context budgeting

**Why:** large repos exceed any context window; prompts currently carry
whole files (truncated). Retrieval keeps evidence high while cost stays flat.

**Shape:** embed-and-retrieve over the repo map; per-role token budgets;
summarised hand-offs on the blackboard instead of raw artifacts.

## 7. Retrospective learning (Reflexion-style)

**Why:** the team should get better *between* runs, not just within one.

**Shape:** after each delivery, a retrospective agent distils what failed and
why into `ProjectMemory`; the manager and engineer prompts consume it (the
plumbing — memory load → prior context — already exists).

## 8. Richer eval benchmark

**Why:** `dev_team.evals` scores file presence and run success. World-class
needs behavioural checks and regression tracking over time.

**Shape:** eval cases with executable assertions (run a command against the
delivered workspace), a small standing benchmark suite in CI against the real
runner (budget-capped, nightly), and score history so prompt/orchestration
changes show up as deltas.

## 9. MCP tool provider & group review

**Why:** specialist agents benefit from real tools (dependency scanners,
linters, issue trackers) and from debate on contentious calls.

**Shape:** expose MCP servers through `allowed_tools`; for high-severity
review disagreements, a short structured debate (reviewer vs engineer,
security as judge) before the verdict is final.
