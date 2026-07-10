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

All of the above are wired into the `DeliveryEngine`, not left as scaffolding.

## Deferred (tracked for a later iteration)

Dynamic re-planning, hierarchical delegation / group-chat consensus, codebase
retrieval (RAG) + context budgeting, checkpoint & resume, MCP tool provider,
CI required-checks integration, and Reflexion-style retrospective learning.
These are valuable but larger; they are intentionally out of this slice.
