# Assessing an existing repository (`--assess`)

The third engine, next to simulation and delivery: point the team at an
existing repository — a legacy monolith, an inherited codebase, a dormant
project — and get back a **cited, phased audit report** instead of a code
change. Assessment is **read-only by construction**: no delivery branch, no
baseline commit, no quality gates, no `.dev_team/` bookkeeping. The auditing
agents get read-only tools (`Read`/`Grep`/`Glob`) rooted at the workspace,
and the only write the run makes is the report itself. (One opt-in
exception: `--build-probe` runs the project's own build for a ground-truth
buildability verdict — see below.)

```bash
dev-team --assess --workspace /path/to/legacy-repo \
    --report audit/2026-07-12_01_legacy-assessment.md \
    "Legacy monolith" "dormant 2-3 years, frontend + backend in one repo" \
    --budget-usd 10 --interactive --verbose
```

No local checkout? `--repo owner/name` clones the repository first (private
repos authenticate with a `GITHUB_TOKEN` read from `--env-file` or `./.env`;
the token is handed to git per-command and stripped from the process
environment, so nothing the audit executes can read it) and uses the clone
as the workspace. A repository already cloned by a previous run is
fast-forwarded, not re-cloned.

The optional title/description scope the audit (they are woven into every
phase prompt); `--report` defaults to `audit/assessment.md` inside the
workspace; `--json` emits the structured outcome instead of the markdown.
Exit code `0` means every phase completed; `1` means a phase failed or the
run was aborted — the report says which.

## The phases

Each phase is one specialist working to a JSON contract that **requires a
file-path citation per claim** and tells the agent to state ambiguity rather
than guess, and to flag code that works around bugs in ancient dependency
versions (upgrades break those).

| Phase | Auditor | Covers |
|-------|---------|--------|
| 1. Inventory | Anders (architect) | languages/frameworks/versions, components, the frontend/backend boundary, entry points, build/CI/deploy artifacts, dead directories |
| 2. Buildability | Devon (DevOps) | could it build today: lockfiles, pinned dependency resolvability, runtime/SDK requirements — **no installs are run** |
| 3. Risk | Sasha (security) | EOL/abandoned/vulnerable dependencies (must-fix vs should-fix), hardcoded secrets, data layer & migration state, external services likely dead |
| 4. Tests & docs | Quinn (QA) | what test infrastructure exists and whether it plausibly runs; documentation vs what the code actually does |
| 5. Recommendation | Priya (product manager) | classification, rationale, the **single highest-risk item blocking a first build**, and a sequenced remediation plan with effort estimates |

Phase 1 runs first; phases 2–4 run **in parallel**, each anchored by the
inventory summary; phase 5 synthesises. Wren (technical writer) closes with
an executive summary. Classifications are fixed vocabulary —
`revive-in-place`, `dependency-surgery`, `strangler-rewrite`, `rebuild`
(build a replacement from scratch; the old system is a requirements
document, not a foundation), or `archive` — anything else fails the phase
rather than being reported as a verdict. The prompt defines each option, so
"upgrade in place", "incremental rewrite", and "big-bang rebuild" are all
first-class answers the audit can give.

Two deterministic anchors keep the agents honest: the repo context (file
tree + manifest heads) and an exact inventory (LOC per top-level directory,
files by extension) are computed in Python, not by a model, and fed into
every prompt. Both appear in the report's appendix.

## Interactive scope check

With `--interactive` (or any `InteractionChannel`), the run pauses once —
after inventory, before the expensive deep phases:

```text
Anders asks: Inventory is done. Adjust the audit scope before the deep phases?
[continue] audit everything  [focus] narrow the scope  [abort] stop the assessment >
```

`focus` takes free text ("skip the frontend, it's being rewritten") that is
appended to the scope of phases 2–5. Unattended runs continue automatically.

## Degradation, not unwinding

A phase whose agent fails (malformed JSON after retries, budget exhaustion)
is recorded as a failed phase **in the report** — the other phases still run
and the report still renders, so a partial audit is never lost. The budget
circuit-breaker, tracer, personas, and roster all work exactly as in
delivery runs.

## .NET support

Assessment was built with legacy .NET monoliths in mind, and the project
machinery understands them now:

- **Profile detection** (`profile.py`): a root-level `.sln`, `.csproj`, or
  `global.json` resolves to the `dotnet` profile (`dotnet test` /
  `dotnet restore`, `dotnet list package --vulnerable` as the scan) — and it
  is checked **before** `package.json`, so a full-stack monolith with
  frontend assets at the root resolves to its solution, not to npm.
- **Repo context** reads .NET manifests (`global.json`, `packages.config`,
  `Directory.Build.props`).
- **Baseline attribution** (`failures.py`) parses VSTest and xUnit failure
  output, so *delivery* runs against .NET repos can tolerate a red baseline
  and gate on newly failing tests only.

## Honest limitations

- **Exactly-pinned dependencies get a live OSV.dev scan; everything else
  is model knowledge** — the report footer says which mode produced the
  claims. Lockfiles (`package-lock.json`, `poetry.lock`, `Cargo.lock`,
  NuGet `packages.lock.json`) are parsed alongside the manifests, so a
  range-specified project still gets its *resolved* versions scanned;
  only dependencies with no lockfile and no exact pin fall back to
  training data. EOL judgments always do — treat those as a triage list,
  not a compliance scan.
- Phase evidence is as good as what the auditors read: on very large repos
  the deterministic inventory is exact, but agents sample files. The report
  appendix names the **audit blind spots** — top-level directories no
  finding cited — so a sampled audit cannot read as a complete one. Narrow
  the scope interactively (or via the description) for depth where it
  matters.
- Buildability is assessed statically by default — nothing is restored,
  installed, or compiled. Opt in to `--build-probe` to ground the verdict
  in real exit codes (see below).

## Build probe (`--build-probe`)

The one opt-in departure from read-only: the detected profile's setup and
verify commands (e.g. `npm install` + `npm test`, `dotnet restore` +
`dotnet test`) are actually executed in the workspace, and their exit codes
and output tails are fed to the buildability auditor as ground truth and
recorded in the report appendix. Commands stop at the first failure —
running the test suite after a failed restore would only bury the signal —
and profiles with no locally runnable commands (legacy .NET Framework)
skip with a recorded reason instead of pretending.

**Safety**: this executes the repository's own build scripts — arbitrary
code — and mutates the working tree the way any build does
(`node_modules/`, `obj/`, lockfile refreshes). It is off by default; only
use it on trusted repositories or run the whole assessment inside a
sandboxed container/VM, exactly as for delivery runs.

## Deterministic analyses (v0.7)

Alongside the agent phases, four deterministic analyses run with no model
involved, so their findings are exact and citable:

- **Dead-code probes** — `.cs` files no legacy MSBuild project references in
  its `<Compile>` items; `.csproj` files no `.sln` includes; top-level
  directories whose last commit trails the repository head by
  `dormancy_days` (default 365). Probes skip themselves with a recorded
  reason when preconditions are missing (no git, SDK-style projects).
- **Live dependency scan** — exact pins from the manifests
  (`packages.config`, `package.json`, `requirements.txt`, `Cargo.toml`)
  and the lockfiles (`package-lock.json`, `poetry.lock`, `Cargo.lock`,
  NuGet `packages.lock.json`) queried against OSV.dev in one batch
  (`--no-osv-scan` opts out; offline degrades to a labelled
  model-knowledge fallback).
- **Audit blind spots** — the exact set of top-level directories no phase
  finding (nor dead-code probe) cited, listed in the report appendix so
  sampling gaps are named instead of implied clean.
- **Component detection** — one component per directory holding a manifest;
  `--component-fanout` runs a parallel per-component deep-dive
  (`max_components` caps it).
- **Convention sources** — `.editorconfig`, ReSharper `.DotSettings`,
  rulesets and linter configs feed the conventions side-phase, whose cited
  profile persists to `.dev_team/conventions.json` for delivery runs
  (`--no-conventions` opts out).

Excludes (`--exclude`, default: vendored/build-output globs) apply to the
tree, statistics, and component detection. `--backlog` converts the audit's
findings into estimated stories in `.dev_team/backlog.json`, deduplicated by
title, under an "Assessment remediation" epic — the input queue for later
`--deliver` runs.

## Library use

```python
import asyncio
from dev_team import AssessConfig, Budget, DevTeam, LocalWorkspace

async def main():
    team = DevTeam()
    outcome = await team.assess(
        workspace=LocalWorkspace("/path/to/legacy-repo"),
        budget=Budget(limit_usd=10.0),
        config=AssessConfig(
            focus="backend only; the SPA is being rewritten",
            report_path="audit/backend-assessment.md",
        ),
    )
    print(outcome.classification)           # e.g. "dependency-surgery"
    print(outcome.phases["risk"].data)      # structured findings per phase
    print(outcome.report_markdown)          # the full cited report

asyncio.run(main())
```
