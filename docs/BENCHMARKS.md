# How each agent is benchmarked — and what "best in field" means

Research snapshot (mid-2026) mapping every dev-team role to the external
benchmarks that define state of the art, the quantitative bar production
systems use, and which techniques this codebase has adopted (✅) or deferred
(→ roadmap). The cross-cutting finding, consistent across all nine roles:
**execution grounding beats text quality, deterministic tools + LLM triage
beat either alone, and false-positive suppression is a first-class metric.**

## Engineer
- **Benchmarks:** SWE-bench Verified (saturated), **SWE-bench Pro** (Scale
  standardized harness ~52–59% — vendor self-reports run ~20pts higher),
  SWE-bench Live, Terminal-Bench 2.x, Aider polyglot, Commit0.
- **Winning techniques:** reproduce-first (failing test before the patch) ✅
  (prompted); execution-guided repair loops ✅ (gate feedback per attempt);
  per-attempt escalation ✅; multi-candidate generation with execution-based
  reranking → roadmap; retrieval-driven fault localization → roadmap;
  session continuity across attempts → roadmap.

## Code reviewer
- **Benchmarks/metrics:** CRScore, SWR-Bench, CR-Bench; production systems
  define "good" as **precision at a fixed comment budget** and
  **actionability** — Google Critique ML (resolved-rate at fixed precision),
  Meta MetaMateCR ("ActionableToApplied" 19.7%), GitHub Copilot review (71%
  actionable at ~5 comments/review).
- **Adopted:** comment budget (6) with actionability rules ✅; static-analysis
  grounding — the reviewer triages `lint_command` output instead of
  re-deriving it ✅; full content + git diff evidence ✅; review rejections
  tracked in the run scorecard ✅.

## QA / test generation
- **Benchmarks:** **SWT-Bench** (fail-to-pass: the test must fail before the
  fix, pass after — top systems ~87–89%), TestGenEval (the only major
  benchmark scoring **mutation score**; models ~35% vs human suites),
  TestEval, Meta's ACH (mutation-guided generation in production).
- **Adopted:** fail-to-pass validation ✅ — after gates pass, the engine
  reruns them with the implementation reverted (snapshot restore or
  `git stash -u`); a suite that still passes is rejected as vacuous
  (`EngineConfig.fail_to_pass_check`). Mutation-lite scoring → roadmap.

## Security
- **Benchmarks:** CWE-Bench-Java/IRIS (neurosymbolic LLM+CodeQL found 2× the
  CVEs of CodeQL alone with a *lower* false-discovery rate), SecRepoBench
  (secure-pass@1 <40%), CyberSecEval 4 / AutoPatchBench, SastBench (LLM triage
  cut a >92% SAST FP rate to ~6%), DARPA AIxCC (find/prove/patch; FPs made
  structurally impossible by requiring proof).
- **Adopted:** SAST + LLM triage ✅ — a scanner (`security_scan_command`,
  auto-suggested per project profile: bandit / npm audit) runs first and the
  agent triages its output; evidence discipline ✅ — blocking findings must
  cite file, code, and attack path. Proof-of-vulnerability → roadmap.

## Architect
- **Benchmarks:** **R2ABench** (PRD→architecture vs expert references:
  structural graph metrics + LLM judge + anti-pattern detection), DevBench
  design phase, ArchBench, SAKE (knowledge MCQs — near-saturated, so knowledge
  isn't the bottleneck). **Verified gaps in the field:** no benchmark links
  design quality to downstream implementation success; no operationalized
  ATAM rubric.
- **Adopted:** prior-ADR context (3–5 recent decisions — "context engineering
  beats model scale", arXiv 2604.03826) ✅; alternatives-and-tradeoffs
  required in every design (ATAM-lite) with rationale persisted into the ADR ✅;
  anti-pattern self-check ✅. In-house downstream metric (attempts-per-task
  per design) → roadmap; this pipeline can measure what academia can't.

## Product manager / planner
- **Benchmarks:** PlanBench/ACPBench (formal planning), INVEST-based
  user-story quality studies, FeatureBench; the emerging consensus metric is
  **decomposition quality = downstream agent success** with validation-gated
  subtask transitions (which the gate-per-task engine already implements).
- **Adopted:** INVEST-hardened prompts (verifiable acceptance criteria) ✅;
  deterministic plan lint (missing criteria, unknown/self dependencies,
  duplicates, oversize) with one revision pass ✅; plan quality visible in
  scorecard + retrospectives ✅. Dynamic re-planning on failure → roadmap.

## Technical writer
- **Benchmarks:** CodeWikiBench (repo-level docs vs rubrics derived from
  ground truth), ReleaseEval, SmartNote; doc-claim verification research
  (Cascade) — naive doc-vs-code consistency checking has unacceptable FP
  rates.
- **Adopted:** docs are shipped artifacts ✅ — real files written into the
  workspace and committed with the feature, grounded in the actual delivered
  code and aware of existing docs. Executable doc-claim checks → roadmap.

## SRE
- **Benchmarks:** ITBench (frontier models <50% on SRE incident scenarios),
  AIOpsLab, OpenRCA 2.0 (scores causal-path groundedness). Production-
  readiness review itself is unbenchmarked; Google's SRE Launch Checklist is
  the de-facto rubric.
- **Adopted:** PRR-rubric review over evidence ✅ — the SRE sees the delivered
  code, gate results, and the deployment plan's rollback, and must ground
  verdicts in what it saw. Incident-response capability → after PR/CI
  integration.

## DevOps
- **Benchmarks:** IaC-Eval / Multi-IaC-Eval (`terraform plan` + intent checks
  as oracles), Repo2Run / Multi-Docker-Eval (build success, fail-to-pass in
  the built environment). Unanimous oracle: **execution, never text quality**.
- **Adopted:** real artifacts ✅ — Dockerfile/CI/service files authored as
  workspace files and shipped in the feature commit, matched to the detected
  project kind. Artifact execution gates (`docker build` as a DoD gate) can
  be added today via `CommandGate`; container-native verification → roadmap.

## The in-house advantage

External benchmarks score agents in isolation. This system owns the whole
pipeline, so it can measure what they can't: the run **scorecard**
(`blackboard["scorecard"]`) tracks plan lint issues, review rejections, gate
failures, and vacuous-test rejections per delivery, and retrospectives feed
those lessons into the next run. Trend these numbers across the eval suite
and agent changes become measurable, not vibes.
