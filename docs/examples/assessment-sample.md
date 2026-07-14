# Example: an assessment report

> **Illustrative sample — not a real audit.** This is a fabricated, redacted
> example of what `dev-team --assess` writes, for a fictional legacy repository
> ("Contoso Billing Portal"). Every secret, host, and identifier is invented or
> redacted. It mirrors the real report structure produced by `render_report`
> in [`assessment.py`](../../src/dev_team/assessment.py): an executive summary,
> a recommendation carrying a fixed-vocabulary **Classification**, the five
> audit phases (inventory, buildability, risk, tests & docs, plus the
> synthesised recommendation), house conventions, and a deterministic appendix
> (exact inventory stats, dead-code and dependency-scan output, audit blind
> spots, and the cost footer).
>
> Each finding also has a **positional id** — `risk.secrets[0]`,
> `recommendation.plan[3]`, `buildability.blockers[1]`, … — which the report
> body does not print but the dashboard and API expose. You re-check any single
> claim with a fresh, skeptical agent via
> `dev-team --verify ./some-repo --finding risk.secrets[0]`. See
> [`docs/ASSESSMENT.md`](../ASSESSMENT.md).

Everything below the line is the report as generated.

---

# Repository assessment

Project profile: **dotnet** (root-level solution `Contoso.Billing.sln`).
Audit scope: whole repository; special attention to upgrade-vs-rewrite for the AngularJS front end.

## Executive summary

Contoso Billing Portal is a Windows-only .NET Framework 4.7.2 monolith that
has not shipped a commit in roughly 26 months. A single ASP.NET project hosts
both server-rendered WebForms pages and an AngularJS 1.5 single-page app, with
domain logic in a companion class library and a second, orphaned reporting
project that no solution references. It does not build today: three NuGet
packages pinned in `packages.config` are no longer available on the configured
feed, and the front-end build assumes a Node 6 / Bower toolchain that is long
gone. A live database connection string with an inline password is committed
to `Web.config`. The code is coherent and the domain is well factored, so the
recommendation is a **strangler-rewrite**: stabilise the build and secrets,
then carve functionality out behind a facade rather than rewrite blind or
attempt a big-bang migration of the dual UI.

## Recommendation

**Classification: strangler-rewrite**
The system is worth keeping — the billing domain is well factored and the
business rules are not written down anywhere else — but the two overlapping UI
stacks and the Windows-only, unbuildable toolchain make an in-place upgrade
disproportionately risky. Stand up a facade, get one vertical slice building
and tested, and migrate screen by screen.

**Highest-risk item blocking a first build:** the solution does not restore — three packages pinned in `packages.config` are absent from the configured NuGet feed, so nothing compiles until the feed and the pins are reconciled.

### Remediation plan

1. Restore the build on a supported toolchain — *M*. Reconcile the missing NuGet pins, migrate `packages.config` to `<PackageReference>`, and confirm a clean `msbuild` restore + build.
2. Remove and rotate the committed secret — *S*. Pull the connection string out of `Web.config`, move it to an environment variable or Key Vault, and rotate the exposed database credential (treat it as compromised).
3. Upgrade the must-fix dependencies — *M*. Newtonsoft.Json 9.0.1 → 13.x, jQuery 1.11.3 → 3.7.x, Bootstrap 3.3.5 → a maintained line; re-test the AngularJS bundle against the upgraded jQuery.
4. Stand up the strangler facade — *L*. Put a reverse proxy in front of the monolith and carve the billing API out behind it as the first migrated slice.
5. Establish a green test baseline — *M*. Get the 41 existing MSTest cases running in CI before any behavioural change, so regressions during migration are visible.

## Phase 1 — Inventory

A single-solution .NET Framework monolith (`Contoso.Billing.sln`, 3 of 4 `.csproj` files referenced) with an AngularJS front end bundled by Gulp. The frontend and backend live in the same deployable unit.

### Components

- **Contoso.Billing.Web** (`src/Contoso.Billing.Web`) [ASP.NET WebForms + MVC] — the monolith host: pages, controllers, and the AngularJS SPA it serves
- **Contoso.Billing.Core** (`src/Contoso.Billing.Core`) [C# class library] — domain model and data access (EF6 + hand-written ADO.NET)
- **Contoso.Reports.Legacy** (`legacy/Contoso.Reports.Legacy`) [C# class library] — batch PDF reporting; present on disk but not in the solution
- **billing-spa** (`web`) [AngularJS 1.5] — the front-end SPA, bundled with Gulp and Bower

**Frontend/backend boundary:** the AngularJS SPA under `web/` is both served by and calls back into the WebForms host; there is no separate API tier — controllers in `Contoso.Billing.Web` return HTML and JSON from the same actions.

### Entry points

- `src/Contoso.Billing.Web/Global.asax.cs` (http-application)
- `src/Contoso.Billing.Web/Web.config` (configuration)
- `web/gulpfile.js` (frontend-build)

### Findings

- Two parallel UI stacks (server-rendered WebForms and the AngularJS SPA) render overlapping billing screens (evidence: src/Contoso.Billing.Web/Views)
- `legacy/` holds a second reporting implementation not wired into the solution or any build (evidence: legacy/Contoso.Reports.Legacy)

## Phase 2 — Buildability

The project cannot build or restore in its current state, on any platform, without first repairing dependency resolution and the front-end toolchain.

**Builds today: no**

### Blockers

- `nuget restore` fails: three packages pinned in packages.config are absent from the configured feed — dependencies (evidence: packages.config)
- Targets .NET Framework 4.7.2 with no `global.json`; requires Windows plus MSBuild / VS Build Tools — toolchain (evidence: src/Contoso.Billing.Web/Contoso.Billing.Web.csproj)
- Front-end build assumes Node 6 and a global `bower install`, neither reproducible today — frontend (evidence: web/package.json)

### Runtime requirements

- .NET Framework — 4.7.2, Windows-only (evidence: src/Contoso.Billing.Web/Contoso.Billing.Web.csproj)
- SQL Server — 2016+ with the Contoso billing schema (evidence: db/schema.sql)
- Node.js — 6.x for the legacy Gulp build (evidence: web/package.json)

## Phase 3 — Risk

Two must-fix items dominate: a committed live credential and a set of dependencies years past end of support. The data layer is conventional but its migration history is incomplete.

### Dependencies

- Newtonsoft.Json — 9.0.1 — must-fix — upgrade to 13.x (known advisory, see appendix) (evidence: packages.config)
- jQuery — 1.11.3 — must-fix — upgrade to 3.7.x (evidence: web/package.json)
- Bootstrap — 3.3.5 — should-fix — move to a maintained line or vendor a fork (evidence: web/package.json)
- EntityFramework — 6.1.3 — should-fix — 6.4.x is a low-risk in-place bump (evidence: packages.config)

### Secrets

- SQL Server connection string with an inline password committed to source control (`Data Source=...;Password=[REDACTED]`) — rotate and remove (evidence: Web.config)
- A storage account key referenced from a production config file — confirm and rotate if real (evidence: appsettings.Production.json)

### Data layer

- EF6 code-first with an incomplete `Migrations/` history: the last migration predates two columns present in `db/schema.sql` (evidence: src/Contoso.Billing.Core/Migrations)

### External services

- PaymentGateway (legacy) — endpoint host is hardcoded and the vendor's v1 API is likely decommissioned (evidence: src/Contoso.Billing.Core/Payments/GatewayClient.cs)

## Phase 4 — Tests & docs

A modest server-side test suite exists but is not currently runnable in CI; the front end is untested. Documentation has drifted from the code.

### Test infrastructure

- 41 MSTest cases under `tests/`; the last CI configuration points at a TeamCity server that is no longer reachable (evidence: tests/Contoso.Billing.Tests)
- No front-end tests: a Karma config is present but references a browser launcher that has been removed (evidence: web/karma.conf.js)

### Documentation vs reality

- README describes a one-command `build.ps1` bootstrap that no longer exists in the repository (evidence: README.md)
- The deployment doc references an Azure pipeline definition that is absent from the repository (evidence: docs/DEPLOY.md)

## House conventions

PascalCase for C# types and members, `_camelCase` private fields, one public type per file with the folder mirroring the namespace; tests mirror the namespace under `tests/`. The AngularJS code uses the `controllerAs`/`vm` style throughout.

- C# naming — PascalCase types and members, `_camelCase` private fields (evidence: .editorconfig)
- File layout — one public type per file; folder path equals namespace (evidence: src/Contoso.Billing.Core)
- Test naming — `Method_State_ExpectedResult` (evidence: tests/Contoso.Billing.Tests)

Machine-readable style configs: `.editorconfig`, `Contoso.sln.DotSettings`

## Appendix — deterministic inventory

Deterministic inventory: 612 file(s).
Lines by top-level entry: src=48210, legacy=15430, web=9820, tools=2140, db=1890, assets=1240, tests=760, docs=430, scripts=380, (root)=210
Files by extension: cs=210, js=95, html=60, aspx=42, sql=22, config=18, css=16, json=14, csproj=9, md=6, png=40

Deterministic dead-code probes (3 finding(s) from unreferenced-sources, orphaned-projects, dormant-directories):
- [unreferenced-sources] src/Contoso.Billing.Web/Handlers/LegacyPdfHandler.cs — on disk but not referenced by any project's <Compile> items
- [orphaned-projects] legacy/Contoso.Reports.Legacy/Contoso.Reports.Legacy.csproj — project file not referenced by any solution
- [dormant-directories] tools/ — last commit 940 day(s) before the repository head — dormant while the repo stayed active

Dependency scan: 47 dependencies parsed from manifests and lockfiles (39 exactly pinned, 8 from a version range — lower bound only, not necessarily the installed version).
Live OSV.dev scan: 3 known vulnerability record(s) affecting them.
- Newtonsoft.Json 9.0.1 (NuGet, packages.config): GHSA-5crp-9r3c-p9vr — https://osv.dev/vulnerability/GHSA-5crp-9r3c-p9vr
- jquery 1.11.3 (npm, package.json): GHSA-gxr4-xjj5-5px2 — https://osv.dev/vulnerability/GHSA-gxr4-xjj5-5px2
- bootstrap 3.3.5 (npm, package.json): GHSA-9v3m-8fp8-mj99 — https://osv.dev/vulnerability/GHSA-9v3m-8fp8-mj99

Audit blind spots — top-level directories no finding cited: `assets/`, `scripts/`. Treat them as unexamined, not as clean.

Citations that don't resolve to a real file — risk: `appsettings.Production.json`. Treat the underlying claim as unconfirmed, not just imprecisely cited.

_Cost: $8.4173. Dependency findings include a live OSV.dev vulnerability scan of the exactly-pinned dependencies; other CVE/EOL observations come from model knowledge._
