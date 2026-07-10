# What a real-world multi-agent dev team needs — research → build

The v0.2 capabilities were chosen from a structured research pass (seven
parallel researchers, each grounded in real frameworks — AutoGen, CrewAI,
MetaGPT, ChatDev, LangGraph, Claude Agent SDK subagents — plus current SWE
practice), then synthesised into a prioritised, testable roadmap.

The through-line: **v0.1 was a linear _simulation_** — agents described work as
JSON and nothing had side effects. Real teams *do* the work, *gate* it on
executable evidence, *coordinate* beyond a fixed pipeline, *remember*, and are
*governed*. v0.2 closes those gaps while keeping every side effect behind an
injectable protocol so the suite stays at 100% branch coverage with no network,
LLM, or real-process calls.

## Dimensions researched

| Dimension | Headline gap in v0.1 |
|-----------|----------------------|
| Roles | Only build-and-ship roles; no security, docs, or reliability |
| Orchestration | Hardcoded linear pipeline; no concurrency or routing |
| Execution | Agents *describe* code; nothing is written or run |
| Quality gates | One LLM review + a self-reported coverage number |
| Memory | Amnesiac — nothing shared or persisted |
| Governance | Cost/turns carried but never accumulated, capped, or traced |
| Delivery | One feature per run; no backlog, iteration, or intake |

## What shipped in v0.2 (mapped to research priority)

| Capability | Priority | Module(s) |
|------------|----------|-----------|
| Real workspace + command execution | P0 | `execution.py` |
| Materialise engineer changes (ChangeApplier) | P0 | `changes.py` |
| Executable gates + Definition-of-Done | P0 | `verification.py` |
| Gate-driven self-repair loop | P0 | `engine.py` |
| Concurrent, dependency-aware scheduling | P0 | `scheduler.py` |
| Shared blackboard + ADR decision log | P0 | `memory.py` |
| Cross-run project memory | P0 | `memory.py` |
| Budget guard (cost/turn circuit breaker) | P0 | `budget.py`, `instrument.py` |
| Structured trace / audit log | P0 | `trace.py`, `instrument.py` |
| Human-in-the-loop approval gates | P0 | `approval.py` |
| Security engineer role + gate | P0 | `agents/security.py` |
| Persistent backlog (epic/story) + iteration planning | P0 | `backlog.py` |
| Policy-as-code guardrails over commands | P1 | `policy.py` |
| Git branch/commit via version control | P1 | `git.py` |
| Technical writer & SRE roles | P1 | `agents/techwriter.py`, `agents/sre.py` |

**Correction (v0.3):** the original version of this note claimed all of the
above were wired into the `DeliveryEngine`. Two were not — the backlog and
cross-run project memory shipped as standalone modules only. As of v0.3 both
are genuinely wired in: the engine records every run into an optional
`BacklogStore` and persists/loads `ProjectMemory` around each delivery.

## What v0.3 added (from a deep review of v0.2)

v0.2's biggest flaw was that the "agents" were one-shot JSON generators: the
engineer never saw the codebase, the reviewer judged summaries instead of
code, QA's coverage number was fabricated, and gates/git ran in the
orchestrator's own working directory instead of the workspace. v0.3 fixed the
foundations:

- Agentic engineer (SDK tools + workspace `cwd`); evidence-based reviewer,
  security, and QA prompts carrying real file content.
- QA authors executable tests that the gates actually run.
- Gates, git, and command execution rooted at the workspace; honest
  dry-run pairing for in-memory workspaces.
- Merge-queue integration: parallel implementation, serialised
  apply→review→test→accept with rollback of failed attempts.
- Single commit per delivery, gated on security approval.
- Graceful budget stops, malformed-JSON retries, checkpoint & resume,
  per-role model routing with final-attempt escalation.
- An eval harness so team quality is a measured pass rate, not a claim.

## Deferred (tracked in docs/ROADMAP.md)

Dynamic re-planning, hierarchical delegation / group-chat consensus, codebase
retrieval (RAG) + context budgeting, MCP tool provider, CI required-checks and
PR integration, container-level sandboxing, per-task git worktrees, and
Reflexion-style retrospective learning. These are valuable but larger; they
are intentionally out of this slice.
