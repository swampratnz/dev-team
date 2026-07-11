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
  back; nothing is committed until security approves.
- ✅ **Behaves like a professional in your repo** — refuses to start on a red
  baseline (inherited breakage never gets blamed on the engineer) or over
  uncommitted work, does everything on a dedicated `dev-team/<feature>`
  branch, authors a `.gitignore`, and stages a curated change set (never
  `git add -A` into the feature commit).
- ✅ **Works on legacy suites** — a tolerated red baseline records the failing
  test identities and gates each task only on *newly* failing tests
  (pytest/go/cargo output attribution).
- ✅ **Knows the codebase** — a deterministic repo map (tree, manifest heads,
  test layout) feeds the planner and architect on brownfield runs, and a
  retrospective of what failed last time feeds the next run's plan.
- ✅ **True parallelism (opt-in)** — `worktrees=True` gives every task its own
  git worktree: implementation *and* gate runs proceed concurrently; tasks
  squash-merge into the delivery branch one at a time with a full gate check
  on the merged state.
- ✅ **Adapts to the project** — the verify command is auto-detected from the
  workspace's manifests (npm / cargo / go / pytest), with optional
  `setup_command` provisioning and per-gate timeouts.
- ✅ **Governed and resumable** — cost budgets stop the run gracefully,
  checkpoints let a later run resume where a crashed or over-budget run
  stopped, and every agent call is traced and metered.
- ✅ **Measurable** — an eval harness (`dev_team.evals`) scores the team on
  fixed benchmark cases so prompt/orchestration changes are judged by numbers.
- ✅ **100% test coverage** (branch coverage) of the orchestration code,
  enforced in `pyproject.toml`. Note what this does and doesn't claim: the
  suite proves the machinery with test doubles; it does not exercise the real
  Claude CLI (see *Testing*).
- ✅ **Ubuntu-ready** — packaged for deployment as a container or systemd unit.

The v0.2 capability set was chosen from a structured research pass across seven
dimensions (roles, orchestration, execution, quality gates, memory, governance,
delivery), grounded in real multi-agent frameworks — see
[`docs/RESEARCH.md`](docs/RESEARCH.md). v0.3 hardened it from a deep review:
see [`docs/ROADMAP.md`](docs/ROADMAP.md) for what is still deliberately out.

---

## Two engines

| Engine | Entry point | What it does |
|--------|-------------|--------------|
| **Simulation** | `DevTeam.develop` / `DevelopmentWorkflow` | Fast, side-effect-free walk through the lifecycle — agents *describe* the work as structured data. |
| **Real delivery** | `DevTeam.deliver` / `DeliveryEngine` | *Does* the work: the engineer works agentically in the workspace (or describes changes that are materialised for it), QA authors executable tests, gates run via a `CommandRunner` rooted at the workspace, independent tasks are implemented concurrently and integrated serially, and budget, tracing, memory, checkpoints, approvals, and specialist review thread through the run. Commits happen once, after security approval. |

The delivery engine picks its mode from the workspace: a `LocalWorkspace`
(real directory) enables **agentic mode** — the engineer reads and edits files
and runs commands in that directory via SDK tools. An `InMemoryWorkspace` runs
**described mode** with an honest `DryRunCommandRunner` (its gate output says
`dry-run: ... not executed` rather than pretending to verify). Override with
`EngineConfig(agentic=...)`.

## The team

| Agent | Role | Responsibility |
|-------|------|----------------|
| `ProductManagerAgent` | Product / delivery | Decompose a request into ordered, acceptance-criteria-bearing tasks. |
| `ArchitectAgent` | Architecture | Produce a technical design: components, tech stack, risks. |
| `EngineerAgent` | Engineering | Implement each task, and address review feedback on retries. |
| `ReviewerAgent` | Code review | Approve work or request changes with severities. |
| `QAAgent` | Quality assurance | Design tests and report pass/fail plus coverage. |
| `SecurityEngineerAgent` | AppSec | Threat-model and security-review the change; block on major/critical findings. |
| `TechnicalWriterAgent` | Docs | Produce user docs, API notes, and release notes. |
| `SREAgent` | Reliability | Assess production readiness: SLOs, runbook, rollback. |
| `DevOpsAgent` | DevOps | Produce a deployment plan with steps and rollback, targeting Ubuntu. |

The simulation engine is the `DevelopmentWorkflow` state machine; the real
engine is the `DeliveryEngine`. Both are wrapped by the `DevTeam` facade.

## Capabilities (v0.2)

Beyond the agents, the real engine composes a set of production-shaped,
individually-testable building blocks — each a small protocol with a real and a
fake implementation:

- **Execution** — `Workspace` (in-memory / local) and `CommandRunner`
  (subprocess / dry-run / fake); `ChangeApplier` writes described changes for
  real; in agentic mode the engineer writes files itself via SDK tools.
- **Quality gates** — `Gate` / `DefinitionOfDone` run tests, coverage, lint,
  type-check, and security scans from *actual* exit codes in the *workspace
  root*, driving a self-repair loop until green. QA authors the executable
  tests the gates run.
- **Orchestration** — a dependency-aware, concurrent `schedule`; parallel
  implementation with serialised, rollback-on-failure integration (a merge
  queue). Per-role model routing and a stronger `escalation_model` for a
  task's final attempt.
- **Memory** — a shared `Blackboard`, `DecisionRecord` (ADR) log, cross-run
  `ProjectMemory` (fed back into planning), and a `CheckpointStore` for
  crash/budget-safe resume.
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
     │  Task DONE (checkpointed)                                │
     └─────────────────────────────────────────────────────────┘
     │
     ▼
 Security ─▶ reviews the aggregate diff  ──▶ gate for the commit
     │
     ▼
 TechWriter / SRE / DevOps ─▶ docs, readiness, deployment plan
     │
     ▼
 git commit (once, only if security approved) ─▶ DeliveryOutcome
```

A task is retried up to `max_task_attempts` times whenever review or the gates
reject it; the failed attempt is rolled back and the engineer receives the
feedback (including real gate output) on the next attempt — optionally on a
stronger `escalation_model` for the final try. If it never passes, the task is
marked `FAILED`, dependants cascade-skip, and the run reports incomplete. A
blown budget or crash leaves a checkpoint; re-running the same feature resumes
from it.

## Architecture

The one and only integration point with the Claude Agent SDK is
`dev_team.sdk.ClaudeAgentRunner`, which implements the tiny `AgentRunner`
protocol (`prompt`, `system_prompt`, `allowed_tools`, `model`, `cwd`).
`allowed_tools` + `cwd` are what turn a call into a real agent loop — the
agentic engineer passes both. Everything above the protocol is testable to
100% coverage without spawning the Claude CLI or making network calls — tests
inject a `ScriptedRunner`. Malformed agent responses are retried once with a
corrective instruction (`EngineConfig.json_retries`) before failing the stage.

```
cli ─▶ team.DevTeam ─▶ workflow.DevelopmentWorkflow ─▶ agents/* ─▶ sdk.AgentRunner
                                                                     ├─ ClaudeAgentRunner (real SDK)
                                                                     └─ ScriptedRunner (tests)
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
- `memory.py` / `backlog.py` — blackboard, ADRs, cross-run memory,
  checkpoints, persistent backlog.
- `budget.py` / `trace.py` / `approval.py` / `policy.py` — governance.
- `scheduler.py` / `ordering.py` — dependency-aware concurrency and ordering.
- `json_utils.py` / `parsing.py` — robust extraction of structured data from
  model output (with contract enforcement: blocking findings force rejection).
- `evals.py` — the benchmark harness.
- `team.py` — the `DevTeam` facade; `cli.py` — the `dev-team` command.

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
```

The Claude Agent SDK requires the Claude Code CLI to be available at runtime;
see the [SDK docs](https://github.com/anthropics/claude-agent-sdk-python).

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

Output as JSON for scripting:

```bash
dev-team "Health endpoint" "Add a /health endpoint" --json
```

Exit codes: `0` success, `1` completed with failed tasks, `2` invalid input.

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
from dev_team import EvalCase, FeatureRequest, evaluate

cases = [
    EvalCase(
        name="health-endpoint",
        request=FeatureRequest("Health endpoint", "Add /health returning 200"),
        expected_files=["src/app.py", "tests/test_health.py"],
    ),
]
report = await evaluate(lambda case: team.make_engine(workspace=...), cases)
print(report.render())   # Evals: 1/1 passed (100%), total cost $0.8412
```

Run the eval suite before and after changing prompts, roles, or orchestration —
if the pass rate or cost regresses, so did the team.

### Safety

The delivery engine executes agent-authored code (that is what running the
tests means). `SideEffectPolicy` and `ApprovalGate` are defence-in-depth, not
containment: for unattended or untrusted runs, put the whole process in a
sandboxed container/VM with no credentials and restricted network. The SDK
permission mode defaults to `acceptEdits` with per-call `allowed_tools`
allowlists; `bypassPermissions` is opt-in.

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
