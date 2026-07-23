# Changelog

All notable changes to this project are documented in this file. Version
sections below are reconstructed from the repository history.

## [Unreleased]

### Assessment
- **Dependency scan now covers Go (`go.mod`) and Ruby (`Gemfile.lock`)**
  (`depscan.py`), closing the "verified EOL, model-knowledge CVE" asymmetry
  #117 left open on these two ecosystems. `parse_go_mod` reads every
  top-level and `require (...)` block entry — Go's module resolution has no
  version-range syntax, so a `require` line is always an exact pin and no
  lockfile is needed; `parse_gemfile_lock` reads `GEM`/`specs:` top-level
  (4-space indented) entries as exact resolved pins, skipping deeper-indented
  dependency-constraint lines. Both register in `_PARSERS` and feed the
  existing `collect_dependencies`/`scan_dependencies`/OSV batch pipeline
  unchanged — no new credential, endpoint, or config. `docs/ASSESSMENT.md`'s
  honest-limitations note is updated accordingly.
- **Dependency scan now parses PEP 735 `[dependency-groups]`** (`depscan.py`),
  completing the growth path #125 named for itself. `parse_pyproject_toml`
  reads the top-level `[dependency-groups]` table alongside the existing
  `[project.dependencies]`/`[project.optional-dependencies]` handling,
  reusing the same `_pep508_pin` `==`-only helper — a group's `str` entries
  are scanned exactly like every other manifest spec; `{include-group =
  "..."}` composition-reference entries are skipped, not resolved, so a
  composition-only group is never misreported as scanned. Every malformed
  shape (a non-table `dependency-groups`, a non-list group value, a list
  entry that's neither `str` nor `dict`) degrades to a skip, never raises.
  `docs/ASSESSMENT.md`'s honest-limitations note is updated accordingly.

### Orchestration
- **A backlog foreman turns ready stories into deliver jobs** (ROADMAP #9's
  second half, completing the item; see `docs/DISPATCH.md`). `GET
  /foreman/plan` is a $0 dry-run; `POST /foreman/run` enqueues
  dependency-ready `todo` stories (selection is pure code —
  `dev_team.foreman.ready_for_delivery`, backlog order, no model) as bounded
  deliver jobs on the existing single-flight queue. Spend is hard-bounded by
  a **required per-story** `budget_usd` × `max_stories` (`[1, 10]`, default
  3); repos resolve through the existing `source_job` → `meta.json`
  provenance chain (a provenance-less story is skipped with a reason, never
  guessed at, unless the body passes an explicit `repo` fallback). Stories
  carry forward provenance (`delivery_job`, serialised only when set) and
  jobs the reverse (`JobSpec.story_id`, surfaced in `GET /jobs`); the
  worker's terminal transition writes the story back under the shared
  backlog lock — `done` only for a genuinely successful delivery, `blocked`
  on failure/timeout/delivered-nothing (one autonomous attempt per story,
  the needs-human posture), back to `todo` when a still-queued job is
  cancelled. Write-backs are best-effort: a backlog write failure never
  takes the single-flight worker down.
- **A front door that routes raw requests** (`--intake "TEXT"`, ROADMAP #9's
  intake half): one bounded `TriageAgent` call picks a mode from the closed
  `TRIAGE_ROUTES` set (`deliver`/`assess`/`chat`, fail-safe `unclear`) and,
  for a delivery, distils the brief. The request text is untrusted and enters
  the prompt as a defused `<intake-request>` block; an out-of-contract route
  or a delivery without a usable brief degrades to `unclear`, never to an
  action. The decision is *proposed* — route, rationale, brief, the exact
  equivalent `dev-team` command, and the triage cost — and only executed
  under explicit `--intake-apply` or an `--interactive` confirmation (new
  `triage-review` question, fail-safe abort on EOF). An applied route falls
  through into the ordinary `--deliver`/`--assess`/`--chat` flow on the same
  budget, so triage spend counts against `--budget-usd`; `--json` emits the
  decision document instead of the text proposal.

### Delivery
- **A run-level design-thoroughness signal is now trended in the score
  history** (#178, closing `docs/BENCHMARKS.md`'s named "in-house downstream
  metric (attempts-per-task per design) → roadmap" gap). Right after a
  successful `architect.design()` call, three scorecard keys —
  `design_components_count`, `design_risks_count`,
  `design_alternatives_count` — are set to the `len()` of the corresponding
  list on that run's `Design`, reusing 100% of the existing
  `ScoreHistory`/`_score_deltas` machinery (no changes to `scores.py`: its
  generic key-union already trends any new scorecard key). A run halted
  before design completes never gets these keys (matching the existing
  "absent, not zero" convention for not-applicable metrics), while a design
  with empty lists correctly gets explicit zeros. This is honestly scoped as
  run-level only: the architect runs once per whole plan with no task
  linkage in the data model, so a true per-task with/without-design
  attempts comparison remains future work pending an architect opt-out flag.
- **A visual-critique failure is now diagnosable instead of a bare "skipping"**
  (#152, root-cause corrected by adversarial review). The default `--deliver`/
  `--assess` progress stream (`cli.py`'s `_progress_printer`) now renders
  `AgentEvent.detail` when present — previously only the full `-v` event log
  ever surfaced it, so `Visual critique failed; skipping` showed no reason at
  all in the default stream even though `engine.py` was already capturing the
  exception into `detail`. This is a general printer fix (every event's
  detail is now visible), not special-cased to visual review. Because an
  `anthropic` SDK exception could in principle echo credential material, and
  `detail` is now operator/log-visible in the default stream, the visual
  critique's exception text is redacted first
  (`engine._scrub_anthropic_credentials`) against any live
  `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`/`CLAUDE_CODE_OAUTH_TOKEN` value —
  the same by-known-value approach `sources.scrub_credentials` already
  established for git output, not a generic secret-pattern heuristic. No
  change to `AnthropicVisualReviewer._make_client()` or credential
  resolution: the evidence traced the original report to the printer gap, not
  a client-construction defect.
- **Project detection now recognises Maven, Gradle, and Composer manifests**
  (#145, the "smarter unknown fallback" ④ deferred by #133/#135): root
  `pom.xml` (`kind="maven"`, `mvn test`), root `build.gradle`/
  `build.gradle.kts` (`kind="gradle"`, `gradle test`), and root
  `composer.json` (`kind="php"`, `composer test`/`composer install`) join the
  five previously recognised kinds, so these repos no longer fall through to
  the `pytest` guess that caused #133's costed .NET incident. The three new
  filenames are added only to `manifest_kind_for_filename`, the single
  source of truth `_detect_nested_manifest` and `engine.py`'s
  `_manifest_signature` re-detection both key off, so depth-1 nested
  detection and re-detection-on-manifest-change work for the new kinds with
  no changes to either. The existing generic `_degrade_if_toolchain_missing`
  (`shutil.which`) likewise covers all three automatically: a runner missing
  `mvn`/`gradle`/`composer` degrades to `locally_runnable=False` instead of a
  `CommandGate` configured to invoke a binary that isn't on `PATH`. Ruby is
  deliberately excluded (no single canonical test command).
- **Session reuse (`--reuse-engineer-session`) now composes with worktree
  mode (`--worktrees`)** instead of silently no-oping. `_develop_task_in_worktree`
  opens one persistent engineer session per task, rooted in that task's own
  worktree, via the same `_open_engineer_session`/`_engineer_attempt`
  machinery `--reuse-engineer-session` already uses outside worktree mode —
  each retry continues the session instead of re-exploring the repo cold. A
  session turn that errors falls back to the proven cold `implement_in_place`
  path for that attempt and every later attempt of the same task; the session
  is always closed before its worktree is removed. Off by default; only
  activates when an operator has opted into both flags.

### Benchmarks
- **The benchmark suite's aggregate result now persists across CI runs**
  (ROADMAP #6 follow-up): a new `dev_team.benchmark_history.BenchmarkHistory`
  mirrors `dev_team.scores.ScoreHistory`'s bounded-JSON-trail, fail-secure-load
  shape over a plain local file (the benchmark harness has no durable
  workspace to attach to). `dev-team-benchmark` gains an opt-in
  `--history-file PATH` flag (unset by default — zero new disk I/O, today's
  behaviour unchanged); when set, it appends the run's pass/cost totals and
  prints the signed pass-rate/cost delta against the prior run. A history
  write failure is caught and never changes the reported exit code.
  `.github/workflows/benchmark.yml` restores/saves `.benchmark/history.json`
  via `actions/cache` around the run — no repo write, `permissions:
  contents: read` unchanged.

### Security hardening
- **A delivery no longer authors `.github/workflows/*` files by default**
  (issue #151): the DevOps agent's prompt still allows it to propose a CI
  workflow, but `DeliveryEngine._provision_deployment` now drops any
  `.github/workflows/*` file from its artifacts before they reach the
  workspace unless the new `EngineConfig.allow_ci_workflows` /
  `--allow-ci-workflows` opt-in is set (off by default), emitting a
  `deployment-artifacts-blocked` event when it does. This closes a
  push-breaking failure mode: a fine-grained GitHub PAT lacks the separate
  `workflow` scope needed to push such a file, so an uninvited workflow could
  turn a fully-committed delivery into a rejected push with no PR opened.
  When the opt-in *is* set, `dev_team.delivery_target.push_branch` /
  `publish_pull_request` now warn (`stderr` by default, overridable via a new
  `warn` callable) about the `workflow`-scope requirement before attempting
  the push, rather than relying on git's own rejection message reaching the
  operator. `dev_team.changes.is_ci_workflow_path` is the shared path rule
  both sites use — it compares the same normalised segments
  `execution._normalise` produces (splitting on both `/` and `\`, dropping
  empty/`.` segments) rather than a literal string prefix, so a differently
  spelled but equivalent path (`.github//workflows/x`,
  `.github/./workflows/x`, or Windows-style `.github\workflows\x`) can't
  disagree with the workspace about what counts as "in `.github/workflows/`"
  and slip an unauthorized workflow file past the default-deny filter.
- **Every agent call now produces a retained, reviewable log, closing a
  CLAUDE.md section 7 gap.** `Tracer` (`dev_team.trace`) gains an optional
  `sink` invoked once per finalised span; the new `dev_team.tracelog.TraceLog`
  wires it to an always-on, bounded `.dev_team/trace.jsonl` journal — one JSON
  line per agent call with `ts`/`run`/`seq`/`kind`/`name`/`status`/`duration`/
  `attributes` (the latter carrying `cost_usd` on the two result paths). A
  `TraceSpan` has never carried prompt/response text, so this is metadata-only
  by construction, not by a redaction filter — safe to leave on by default,
  unlike the raw-content `--record-transcripts` (see `docs/TRANSCRIPTS.md`).
  Wired in `cli.py`'s `--deliver`/`--assess` and in `dispatch.py`'s per-job
  run, under the same run id as `events.jsonl`; a write failure is swallowed
  rather than breaking the run it is auditing.
- **Visual review now flags its unsandboxed served app.** `--sandbox` only
  boxes gate/build-probe commands via `ContainerCommandRunner` — the app
  `SubprocessAppServer` serves for visual review (`--visual-review`) is a
  bare host subprocess either way. `DeliveryEngine._visual_review` now emits
  a once-per-run advisory event when `EngineConfig.sandbox` is set, so an
  operator combining `--visual-review --sandbox` sees the gap instead of
  silently assuming full coverage. Purely informational: never raises, never
  affects `DeliveryOutcome.success`, and never gates or skips the review
  itself. `docs/SANDBOX.md`'s trust-boundary table and `docs/ROADMAP.md` item
  1 gain matching notes.
- **Prompt-fence defusing is now systemic.** Untrusted content shown to
  agents inside delimited `<...>` blocks (file bodies, diffs, tool/scanner
  output, the cross-run memory digest, the retrospective run digest, audit
  finding claims) is passed through a single shared helper (`dev_team.fences`
  `defuse`) that neutralises the block's own closing tag with a zero-width
  space, so a hostile string can no longer close the block early and have
  what follows read as trusted instructions. The two prior ad-hoc
  implementations (`context.py`, `retrieval.py`) now delegate to it, and the
  previously-unprotected sites (reviewer diff/file/static-analysis, security
  scanner output, planner prior-context and replan evidence, architect
  repo-context, retrospector evidence, assessment repo-context and
  finding-claim) are covered.
- **The dashboard now fails closed on an unauthenticated non-loopback bind**
  (issue #169), closing the exact gap `docs/SECURITY.md` named under "What
  this does NOT protect against": binding `--dashboard` beyond
  `127.0.0.1`/`localhost` with no `DEV_TEAM_DASHBOARD_TOKEN` set used to only
  print a stderr warning and start the server anyway. `_serve_dashboard` now
  raises `DevTeamError` in that case instead — the server is never
  constructed, matching `--dispatch`'s existing hard-fail-on-missing-token
  posture — surfacing via `main()` as exit code 2 with a message naming both
  remediations. The new `--allow-unauthenticated-dashboard` flag (only valid
  with `--dashboard`) opts back into the old warn-and-serve behavior for an
  operator who has already secured the network another way. The loopback
  case, and the loopback-with-transcripts warning, are unchanged — this is
  scoped strictly to the one branch SECURITY.md flagged as unhardened.
  `docs/SECURITY.md` and `docs/DASHBOARD.md` are updated to match.

### Self-improvement pipeline
- **A supervised multi-loop development pipeline now extends this repo
  itself** (`docs/PIPELINE.md`), ported from the community-agent repo's
  battle-tested pipeline: scheduled research/adversarial/orchestrator
  routines coordinate through GitHub issue labels
  (`proposal`/`status:*`/`needs-human` — created by the idempotent
  `setup-labels` workflow + `scripts/setup-labels.sh`), and event-driven
  GitHub Actions do the code work — a build worker
  (`pipeline-build.yml`, fires on `status:approved`, runs the exact CI
  gate — `ruff check .` + the 100% branch-coverage `pytest` — before
  opening a "Closes #N" PR, with a deterministic PR-produced verify step),
  a read-only security-focused PR reviewer (`pipeline-pr-review.yml`,
  verdict posted deterministically from the execution log), and three
  bounded push-exception loops: autofix on CI failure
  (`pipeline-pr-autofix.yml`, from run_attempt ≥ 2, 2 attempts), a
  Changes-requested reviser (`pipeline-pr-revise.yml`, 2 attempts,
  dispatched by the reviewer), and a two-hop merge-conflict resolver
  (`pipeline-pr-conflict.yml`, one attempt, payload carries PR numbers
  only with eligibility re-verified from the API). `pipeline-build-retry`
  and `ci-retry` give failed runs bounded machine reruns before any agent
  or human is spent; everything else escalates `needs-human`.
- **Guardrails are structural**: least-privilege `--allowedTools` (exact
  `git push origin HEAD`, no blanket `git:*`/`gh:*`/`python:*`, no
  `gh pr merge`/`gh api`), `persist-credentials: false` + per-step
  GH_TOKEN so agents reading untrusted content never hold a repo token,
  fork PRs excluded everywhere, attempt caps via marker comments, and
  **no loop merges — humans merge** (reinforcing the BPG standards, which
  also gained a "Multi-loop pipeline" section in `CLAUDE.md`).
  `docs/VISION.md` defines the proposal rubric and the explicit
  do-not-propose list (no merge autonomy, no credential-handling changes,
  no weakening the coverage gate). All agent workflows are inert until the
  `CLAUDE_CODE_OAUTH_TOKEN` secret and Claude GitHub App exist.

### Interactivity
- **The CI-fix loop can now be supervised from the pull request itself**
  (`--interactive-pr-comments`, ROADMAP #7): a new `InteractionChannel`,
  `GitHubPRCommentChannel` (`dev_team.pr_comment_channel`), posts the
  `ci_fix_question` as a PR comment and polls (bounded, injectable `sleep`,
  mirroring `watch_checks`) for a reply from an **explicitly configured**
  allow-list of GitHub logins (`--interactive-pr-comment-author`,
  repeatable) — no implicit "defaults to the PR author". A reply's first
  whitespace-trimmed, lower-cased token must exactly match a live choice
  key (`apply`/`skip`); an unauthorized commenter or an unrecognised reply
  is silently skipped, and an exhausted poll fails safe to `skip`, exactly
  like `ConsoleChannel`'s EOF behaviour. `--interactive-pr-comments`
  requires `--interactive`, `--pull-request`, `--watch-fix-rounds > 0`, and
  at least one `--interactive-pr-comment-author`; it replaces only the
  CI-fix loop's channel — `team.interaction` (plan review, approvals) is
  untouched, and omitting the flag leaves `_run_ci_fix_loop` exactly as
  before. `DeliveryOutcome.pull_request_number` (alongside the existing
  `pull_request_url`) is set from the opened PR so the channel can address
  the comments API, which is keyed by PR number, not URL. Enabling this
  posts the CI failure summary as a plain, repo-visible PR comment — see
  `docs/INTERACTION.md` for the exposure-audience tradeoff before turning
  it on.

### Finding re-verification
- **A fresh skeptical agent can re-check any ONE persisted assessment
  finding** against the code (`docs/ASSESSMENT.md`): `list_findings`
  enumerates the LLM phases' claims from `.dev_team/assessment.json` with
  positional ids (`risk.secrets[0]`; component deep-dives nest), and
  `verify_finding` runs a security-engineer-disciplined verifier — never
  the claim's author — with read-only tools (`Read`/`Grep`/`Glob`) and
  refute-first instructions, returning exactly
  `confirmed|refuted|needs-context` plus rationale and citations. The
  claim under review is treated as untrusted (delimited) content;
  out-of-contract verdicts degrade to `needs-context`. Deterministic
  `dead_code`/`dependency_scan` outputs are excluded — they are program
  results, not model claims.
- **CLI**: `dev-team --verify DIR --finding <id-or-claim-substring>`
  (`--json`, `--budget-usd`). Runs an agent, so it sits behind the
  credential preflight — unlike `--make-backlog`.
- **Dispatch** (`docs/DISPATCH.md`): a `verify` job mode (validated
  synchronously against disk at submit time) plus
  `GET /jobs/{id}/findings` and `GET /jobs/{id}/verifications`. Assess
  runs now mirror `audit/<id>/meta.json` (repo identity) beside the
  assessment JSON, and verify verdicts append to
  `audit/<source>/verifications.jsonl` — all disk-keyed, so the whole
  flow survives a service restart.
- **Verdict calibration** (`docs/DISPATCH.md`): `GET /calibration`, a pure,
  $0, disk-only aggregate rolling up every persisted
  `audit/*/verifications.jsonl` across jobs into per-phase and overall
  `confirmed`/`refuted`/`needs_context` counts with a `confirm_rate` —
  the cross-job rollup `docs/VISION.md` names as the verdict-calibration
  gap left after per-job re-verification (PR #25) shipped. No agent calls;
  an out-of-contract verdict or non-string `finding_id` is dropped, not
  trusted, same as the write-time fail-secure posture.
- **`POST /jobs/{id}/cancel`** (`docs/DISPATCH.md`): the missing rung on
  the job lifecycle — a still-`queued` job can now be pulled out of the
  single-flight queue (`queued → cancelled`, refused with `409` once a job
  is `running` or already terminal) instead of the only prior options
  (wait for it to run anyway, or restart the whole service and lose every
  other queued job). Cancelling is $0 and strictly cheaper than letting a
  job run: the targeted job never reaches `run_job`, so no clone, no
  workspace, no agent call happens for it, and it stops counting toward
  the queue cap. `GET /jobs/{id}/result` on a cancelled job answers
  `{"kind":<mode>,"success":false,"error":"cancelled","cost_usd":0}`. No
  new auth surface, no new store — reuses the existing bearer gate and the
  in-memory registry's lock, shared with the worker's `queued → running`
  flip so the two transitions are mutually exclusive.
- **`POST /jobs/{id}/purge`** (`docs/DISPATCH.md`): permanent, **archive-
  gated** deletion — the follow-up #35 deferred as its own proposal.
  Requires the job to already be `archived` (`409` otherwise), turning
  irreversible deletion into an explicit two-step action. Removes exactly
  three things: the job's workspace clone (`jobs_root/{id}`), the
  `audit/{id}/` mirror (`assessment.md`/`assessment.json`/`meta.json`/
  `verifications.jsonl`, each removed through the traversal/symlink-escape-
  checked `Workspace.delete()` — never a raw filesystem call), and backlog
  stories bred from that job (same write lock `DELETE /backlog/story/{id}`
  uses). `events.jsonl`/transcripts are out of scope for v1. Not idempotent:
  a second purge on the same id is `404`, never a redundant `200`. The
  terminal-state check reads `record.state` directly within a single locked
  block rather than reusing `archive_job`'s internal helper (which itself
  acquires the same lock and would deadlock the single-flight dispatcher).
  The dashboard gained a "delete permanently" button on each archived run
  row, behind the same two-step confirm the backlog's story-delete already
  uses (`docs/DASHBOARD.md`).
- **`POST /jobs/{id}/purge` now also removes recorded transcripts**
  (`docs/DISPATCH.md`, `docs/TRANSCRIPTS.md`): completes the transcript
  half of the "fold `events.jsonl`/transcript surgery into the purge" follow-up
  the original purge PR pre-scoped and deferred. `Workspace` gains
  `delete_dir(path)` (implemented on both `LocalWorkspace` and
  `InMemoryWorkspace`), routed through the same traversal/symlink-escape
  guard every other `Workspace` method uses; `purge_job` calls it against
  `.dev_team/transcripts/{id}/` in the dashboard workspace, and the
  `removed` response dict gains a `transcripts` field alongside `workspace`/
  `audit`/`backlog_stories`. Closes a real gap: when `--record-transcripts`
  and a `--dashboard-workspace` are both configured, transcripts land in
  the dashboard workspace rather than the job's own clone, so the prior
  purge (which only ever touched the job's clone and the `audit/{id}/`
  mirror) silently left them behind. `events.jsonl` remains explicitly
  out of scope — still a separable, harder surface (append-only,
  size-bounded, shared across jobs) than a directory delete.
- **Spend rollup** (`docs/DISPATCH.md`): `GET /costs`, a pure, $0,
  in-memory aggregate summing `cost_usd` across every `succeeded`/`failed`
  job into `total_usd`, `by_mode`, and `jobs_counted`. Unlike
  `GET /calibration`, this rolls up the in-memory registry rather than
  disk — `deliver` job cost is never mirrored to disk, so a disk walk
  would silently under-report. Respects the same archived-exclusion
  (`?archived=1`) as `GET /jobs`, and needs no dashboard workspace to
  answer (archived-exclusion simply no-ops without one).
- **Access log** (`docs/DISPATCH.md`, `dev_team.accesslog.AccessLog`): the
  dispatch service now persists a bounded, retained request/auth trail —
  closing the CLAUDE.md section 7 log-gap left by `Handler.log_message`'s
  deliberate no-op. Every request — `/health`, authorised, `401`, unknown-
  path `404` — appends exactly one `{ts,method,path,status}` record to
  `<jobs_root>/access.jsonl`, created lazily on first request and rewritten
  past 4000 lines to keep the newest half, mirroring
  `dev_team.eventlog.EventLog`'s bound. Never logs the `Authorization`
  header value or any request/response body; the persisted `path` is
  truncated to 2048 bytes independent of the HTTP server's own request-line
  cap. A log-write failure (disk full, unwritable jobs root) is swallowed
  at the handler level and never affects a response already sent to the
  caller.
- **`GET /access-log`** (`docs/DISPATCH.md`): the "natural growth" the
  access-log write path (above) explicitly deferred — a newest-first,
  `?limit=`-bounded (default 100, clamped to `[1, 1000]`) page of the same
  journal, so an operator can glance at recent requests from the dashboard
  instead of SSHing into the tailnet-only deployment to `cat
  access.jsonl`. Reuses the existing bearer auth (no new credential),
  reads straight off `jobs_root/access.jsonl` (no dashboard-workspace
  guard needed, exactly like `GET /costs`), and inherits
  `read_access_log`'s tolerant-read contract (missing file → `200
  {"entries":[]}`, a corrupt line skipped rather than fatal). Every
  returned entry carries exactly the four fields the write path persists —
  never an `Authorization` header or a request/response body, since #54
  never wrote those in the first place.
- **`citation_broken` on every enumerated finding** (`docs/DISPATCH.md`):
  the follow-up the $0 `broken_citations` check (above) named and deferred
  on purpose — `list_findings` now joins its own already-persisted
  `broken_citations` (per-phase, keyed the same way `list_findings`
  enumerates) onto each finding's `evidence`, so `GET /jobs/{id}/findings`
  surfaces which findings are already known, at $0, to be citing a
  fabricated path — before anyone spends a real `verify` job checking one.
  Pure in-memory dict lookup, no new I/O or agent call. Fail-secure: a
  missing `broken_citations` key (assessments persisted before #42), a
  phase absent from it, a malformed (non-list) per-phase value, or empty
  `evidence` all degrade to `citation_broken: false` rather than raising or
  over-flagging — under-flagging is the accepted direction, never promoted
  to a positive signal. `find_finding` inherits the field for free via its
  existing delegation to `list_findings`.
- **Opt-in `--skip-broken-citations` / `skip_broken_citations` acts on
  `citation_broken`** (`docs/ASSESSMENT.md`, `docs/DISPATCH.md`): the named
  follow-up above finally spends — `verify_finding` now short-circuits to a
  $0 `needs-context` result, with no agent/runner call at all, when the
  flag is set and the finding is already known to cite a broken path.
  Default off (byte-for-byte identical behaviour for every existing
  caller); a broken citation only impugns the citation, so the verdict is
  deliberately `needs-context`, never `refuted`. Threaded through the CLI
  (`--verify --skip-broken-citations`) and dispatch (`POST /jobs`
  `mode: "verify"`, `skip_broken_citations: bool`, rejected with `400` if
  not a bool). `Dispatcher.run_job` also skips the repo clone itself
  whenever the eligible skip fires — a genuine $0, no-clone-read result,
  not just a saved agent call — instead of paying `clone_or_update`'s
  network/disk cost for a repo the skip path never reads. A skipped
  verification is never appended to `verifications.jsonl` and never
  counted by `GET /calibration` — no model ever adjudicated it — and
  `GET /jobs/{id}/result` marks a skip with `"success":true,"skipped":true`
  so a caller can tell it apart from a real agent verdict.
- **Interactive dispatch deliver** (`docs/DISPATCH.md`): `POST /jobs` gains
  opt-in `interactive`/`interactive_timeout_seconds` fields — the missing
  wiring `docs/ROADMAP.md` item 7 named directly (`Dispatcher.run_job`
  hardcoded every `deliver` job's `DevTeam(interaction=None)`, so plan
  review, re-plan supervision, and failure escalation always ran fully
  autonomous no matter what an operator wanted). A new `_TrackedChannel`
  (a `QueueChannel` that records its live pending `Question` without
  draining the queue only the engine's own `ask()` should consume) is
  wired in when `interactive: true`, surfaced over two new endpoints:
  `GET /jobs/{id}/question` (peek the live pause, `404` unknown job) and
  `POST /jobs/{id}/answer` (`choice` validated against the closed set of
  the *live* question's keys — never free-form — `400` on a mismatch,
  `409` when nothing is pending, `202` on success). Both reuse the
  existing bearer-auth gate exactly like every other route.
  `interactive_timeout_seconds` resolves to `300` when omitted and is
  clamped to `[30, 1800]` before a `_TrackedChannel` is ever constructed —
  mirroring #71's poll-timeout clamp, so a misconfigured or malicious huge
  timeout cannot wedge the single-flight worker on one paused job; nobody
  answering within the bound falls through to the question's default
  choice, exactly the existing `QueueChannel` fail-secure behaviour. The
  deliver approval gate (`PolicyApprovalGate(block_risks=("high",))`) is
  completely untouched by this feature — answering an interactive question
  can never approve a push/deploy/rm. Zero marginal cost when `interactive`
  is omitted (the default): no `_TrackedChannel` is constructed and
  `interaction=None` is passed exactly as before.
  **Race-free answer delivery**: `_TrackedChannel` mints a fresh single-use
  reply slot per `ask()` call instead of reusing one `QueueChannel.replies`
  queue for the channel's whole lifetime — `answer_question` validates
  `choice` against, and delivers it to, that exact live `(question, slot)`
  pair atomically under the channel's own lock (`submit_reply`), so a reply
  can never be misdelivered to a later, unrelated question if the original
  one has since timed out and moved on.
- **Opt-in N-way adversarial voting for `--verify`** (`docs/ASSESSMENT.md`,
  `docs/DISPATCH.md`): a single `verify_finding` call is itself one
  stochastic LLM sample standing in for the system's confidence. A new
  `votes` parameter (default `1`, byte-for-byte unchanged behaviour) runs
  `votes` independent `SecurityEngineerAgent` passes concurrently — no
  agent sees another's answer — and takes the plurality verdict; a tie
  (e.g. 1-1 at `votes=2`) resolves to `needs-context`, the same fail-secure
  posture already applied to an out-of-contract verdict. `budget` is shared
  and enforced across every pass exactly as for today's single call: if
  some passes exhaust it, the passes that did complete still decide the
  result, and only an all-fail run returns the existing failure shape. The
  result gains `votes`/`vote_count` only when `votes > 1` — additive-only,
  so `GET /calibration` (reading only the top-level `verdict`) needs no
  change. Threaded through the CLI (`--verify-votes N`) and dispatch
  (`POST /jobs` `mode: "verify"`, `votes: int`). Both surfaces cap `votes`
  at **5** via one shared `MAX_VERIFY_VOTES` constant — added at adversarial
  review to close a concurrency-burst gap: uncapped, a dispatch caller
  could fan out an unbounded burst of concurrent agentic passes against the
  shared Max pool in one request, which the per-call `budget` ceiling does
  not itself bound (it caps eventual spend, not the concurrency burst of
  starting many calls at once). Dispatch validation rejects a non-integer
  (including a bool, since `bool` is an `int` subtype in Python) or
  out-of-range `votes` with `400`, never coerced.
- **Opt-in `--verify-expected-hash` / `expected_hash` guards against
  verifying the wrong finding** (`docs/ASSESSMENT.md`, `docs/DISPATCH.md`):
  `list_findings` already mints a short content hash per finding
  specifically so a caller can detect a claim drifting between enumeration
  and verification, but nothing read it back — `find_finding` (exact id,
  else first case-insensitive claim-substring match) was simply trusted.
  Two silent-misverification paths this closes: a re-run `--assess`
  overwriting `.dev_team/assessment.json` (no versioning, single path)
  between a caller noting a finding's hash and later verifying by that id;
  and two findings sharing claim wording, where a remembered substring
  silently resolves to the *other* one. A caller who has a hash to assert
  now gets a fail-secure mismatch check — raised *before* the
  budget-spending agent call — instead of a real verdict for a claim they
  never meant to check. Omitting it (the default) is byte-identical to
  today. CLI: `--verify-expected-hash HASH` (only valid with `--verify`),
  raising the same "no matching finding" error class on mismatch. Dispatch:
  optional `expected_hash` string on the verify `POST /jobs` body,
  rejected with `400` if non-string and `404 {"error":"finding not
  found"}` on mismatch — the same bucket a genuinely-missing finding gets,
  since either way the finding the caller meant isn't there; the job never
  reaches the queue.

### Dashboard
- **`dev-team --dashboard` serves a local web dashboard over the
  workspace** (`docs/DASHBOARD.md`): one card per agent with its current
  stage and last activity, a live feed, recent runs, the backlog with
  story-point progress and status chips, cross-run memory (retrospectives,
  ADRs), captured conventions, and in-place viewing of assessment reports.
  Stdlib-only (`http.server`), read-only over the workspace, self-contained
  page (no external assets), localhost by default (`--port` / `--host` to
  widen — it is unauthenticated, so only on trusted networks). A JSON API
  (`/api/state`, `/api/report`) backs the page and is usable by other
  tooling.
- **Runs journal their progress**: every `--deliver`/`--assess` run appends
  timestamped events (role, stage, message, persona, run id) to
  `.dev_team/events.jsonl` — bounded, corruption-tolerant — which is what
  the dashboard reads. Library users get the same via `EventLog` composed
  into the engine `listener`.
- **Verdict calibration panel** (`docs/DASHBOARD.md`): the dashboard now
  renders the same per-phase/overall confirmed/refuted/needs-context rollup
  `GET /calibration` computes, next to House conventions — computed
  in-process from `audit/<id>/verifications.jsonl` on the shared workspace
  tree rather than proxied to the dispatch service, so it works standalone.
  Respects the same `include_archived` exclusion as the rest of the page;
  a zero-verification workspace renders a muted empty state, not an empty
  table.
- **Spend panel** (`docs/DASHBOARD.md`): a new `GET /api/costs` route
  proxies the dispatch service's `GET /costs` spend rollup — unlike
  calibration, `deliver` job cost is never mirrored to disk, so this has
  to be a proxied read of the dispatch registry, not an in-process disk
  computation. Renders total spend plus a per-mode breakdown next to
  Memory & conventions, fetched once on page load and on manual refresh
  only — deliberately kept out of the existing 2.5s `/api/state` poll, so
  it doesn't multiply dispatch-service load per open dashboard tab.
  Without `--dispatch-url`/`DEV_TEAM_DISPATCH_TOKEN` configured it answers
  `501` and the panel shows a muted "not configured" state. Scope is
  strictly `/api/costs` (exact match, no path parameter) — the same
  narrow-proxy discipline as the existing backlog/job-lifecycle proxies.
- **Access log panel** (`docs/DASHBOARD.md`): a new `GET /api/access-log`
  route proxies the dispatch service's new `GET /access-log` (above) — same
  narrow-proxy shape as Spend (`?limit=` forwarded unchanged, `501
  {"error":"access log not configured"}` without a dispatch token wired).
  Renders a compact, status-colour-coded table of recent requests next to
  Spend, fetched once on page load and on manual refresh only — kept out of
  the 2.5s `/api/state` poll for the same load-multiplication reason Spend
  is. A logged `path` is arbitrary caller-supplied input, so every field
  renders through `esc()` before `innerHTML`, never raw.
- **Pending-question panel** (`docs/DASHBOARD.md`): the dashboard's answer
  to `docs/ROADMAP.md` item 7's "questions as buttons" — a live "pending
  question" panel on each running job's card, backed by two new narrow
  proxies alongside the spend rollup: `GET /api/jobs/{id}/question` (reads)
  and `POST /api/jobs/{id}/answer` (a body-forwarding write, kept separate
  from the no-body archive/unarchive/purge action set). An operator running
  an `interactive: true` deliver job can now see the paused prompt/context
  and click a choice (or type free text for an `accepts_text` choice)
  straight from the dashboard, instead of `curl`ing the dispatch API by
  hand. Polling is scoped and visibility-gated — only currently-running,
  non-archived jobs are polled, only while the tab is visible, on their own
  5s interval kept out of the 2.5s `/api/state` poll (same reasoning as the
  Spend panel) — so the common case (no interactive job running) costs zero
  extra dispatch calls. Both routes reuse the dashboard's existing
  `_authorised()` gate and the server-side-only dispatch token injection;
  neither is reachable without dashboard auth, and the dispatch token never
  reaches the browser. Without `--dispatch-url`/`DEV_TEAM_DISPATCH_TOKEN`
  configured both answer `501` and the panel renders nothing, matching
  Spend/Calibration's degrade-gracefully contract.
- **Report quality chips** (`docs/DASHBOARD.md`): the Reports panel now
  surfaces each audit's already-computed `blind_spots`/`broken_citations`
  counts (`assessment.py`) as chips, instead of leaving them buried in
  report prose — a new `_report_meta_state` reads `audit/<id>/
  assessment.json` in-process (same "no dispatch proxy" pattern as
  calibration) and folds an additive `report_meta` key into
  `collect_state`/`GET /api/state`, no new HTTP route or write. Each
  metric's chip renders independently (a report can have one signal
  without the other); a missing or malformed `assessment.json` omits the
  job from the map rather than fabricating a misleading "0". The report
  modal also prepends an "Audit quality" block listing each blind spot and
  broken citation verbatim — `broken_citations` values are a model's own
  claimed evidence string, so both fields go through `esc()` before
  `innerHTML`, same as the Access log panel's precedent.
- **`GET /calibration` and the Verdict calibration panel fold in
  report-quality totals**, completing the "Natural follow-ups" aggregate
  the Report quality chips (above) explicitly deferred: `blind_spot_total`,
  `broken_citation_total`, and `report_quality_jobs_counted` are summed
  across every non-archived job's `audit/<id>/assessment.json`, kept
  separate from the existing `jobs_counted` (a freshly-assessed job may
  have zero verifications yet, and a `deliver` job has neither file). A job
  with no `assessment.json`, malformed JSON, or wrong-typed
  `blind_spots`/`broken_citations` contributes `0` and is excluded from
  `report_quality_jobs_counted`, never fabricating a misleading "0" —
  `dashboard.py`'s `_calibration_state` gains the identical fields so the
  API and its in-process dashboard mirror never drift. The panel renders a
  one-line summary above the verdict table whenever either total is
  non-zero, even with zero verifications recorded; renders nothing when
  both are zero. No new route, no new write path.
- **Foreman plan panel** (`docs/DASHBOARD.md`): a new `GET /api/foreman/plan`
  route proxies the dispatch service's `GET /foreman/plan` backlog-foreman
  dry-run (see the Orchestration entry above) — the fifth read-only proxy of
  this shape, closing the one gap where a shipped aggregate dispatch route
  had no dashboard panel. Renders ready-story count and one row per
  candidate story (repo, or its ineligibility reason) next to Access log,
  fetched once on page load and on manual refresh only — kept out of the
  2.5s `/api/state` poll for the same load-multiplication reason Spend/Access
  log are. `?max_stories=` is forwarded unchanged, letting the dispatch
  service's own `[1, 10]` clamp handle it; without a dispatch token wired it
  answers `501 {"error": "foreman plan not configured"}` and the panel shows
  a muted "not configured" state. `POST /foreman/run` remains deliberately
  unwired — a spend-multiplying write that needs its own budget/confirm-step
  design, not bundled into this read-only visibility slice.
- **Score history panel** (`docs/DASHBOARD.md`): the dashboard now surfaces
  ROADMAP #6's `dev_team.scores.ScoreHistory` trail, wiring the last
  mechanism it built ("are deliveries getting better over time") into the
  operator dashboard for the first time — previously only visible via a
  shell on the box. A new `_score_history_state` reads
  `.dev_team/score-history.json` in-process (same pattern as
  Memory/Conventions/Calibration, no dispatch proxy) and folds an additive
  `score_history` key into `collect_state`/`GET /api/state`; no new HTTP
  route, and only `ScoreHistory.load()` is ever called, never `.record()`,
  so the dashboard cannot create or mutate score-history entries. Renders
  the last 8 runs, newest first, next to Verdict calibration in the Memory
  & conventions panel — each with its success/failure, task count, attempt
  count, cost, and a signed delta against the run before it in the trail.
  `feature` is the one caller-influenced field (the delivered feature's
  free-text name), rendered through `esc()` before `innerHTML` like every
  other panel; a workspace with no recorded runs shows a muted empty state.

### Sources
- **`--repo owner/name` fetches the repository itself** (also full HTTPS /
  SSH / `file://` URLs): the ref is cloned into the workspace — per-repo
  directory under `./build/` by default, or exactly `--workspace` — and an
  existing clone of the same remote is fast-forwarded instead of re-cloned
  (anything else at the destination is refused, and local changes fail the
  update loudly rather than being overwritten). Valid with `--assess`,
  `--deliver`, and `--chat`.
- **The env file is configured once and found automatically**: without
  `--env-file`, the default search checks `./.env`, then
  `$XDG_CONFIG_HOME/dev-team/dev-team.env`
  (`~/.config/dev-team/dev-team.env`), then `/etc/dev-team/dev-team.env` —
  no per-run flag needed. The fetch line on stderr names the env file a run
  used, so credential debugging never involves guessing.
- **PAT auth with strict token hygiene**: `GITHUB_TOKEN`/`GH_TOKEN` is read
  from an env file (see the default search above) or, failing
  that, taken *out of* the process environment. git receives the credential
  through per-command `GIT_CONFIG_*` variables (an `http.extraheader`
  basic-auth header) — never the URL — so nothing token-shaped lands in
  argv, `.git/config`, process listings, or the environment of any command
  the engines execute (gates, build probes, delivered code). Clone errors
  are scrubbed and a 404 explains that GitHub hides unauthorized private
  repositories.
- `CommandRunner.run()` grew an optional `env` overlay for exactly this
  kind of single-command secret; `GuardedCommandRunner` forwards it only
  when set, so pre-existing custom runners keep working.

### Assessment
- **Live EOL/support-status scan extended to Ruby and Go runtimes**
  (`eolscan.py`): `.ruby-version` (pyenv/rbenv convention) and `go.mod`'s
  `go` directive (e.g. `go 1.21.3`) are now parsed alongside the existing
  Node.js/Python/.NET manifests and checked against endoflife.date the
  same way — one request per distinct detected product, degrading to
  `unknown`/model-knowledge on a malformed manifest or a failed query,
  never guessed. No change to the scan orchestration, HTTP fetch, cycle-
  matching, or verdict logic — just two new parser functions registered
  in the existing `_PARSERS` table (issue #117, follow-on to the original
  three-runtime scan above).
- **Live EOL/support-status scanning via endoflife.date** (`eolscan.py`,
  mirroring `depscan.py`'s shape): Node.js/Python/.NET runtime versions
  parsed deterministically from `package.json` (`engines.node`),
  `.nvmrc`, `runtime.txt`, `.python-version`, or `global.json`
  (`sdk.version`) are checked against endoflife.date, one request per
  distinct detected product. Offline, a failed query, or an unresolved
  release cycle degrades to a labelled model-knowledge/`unknown`
  fallback rather than guessing; `--no-eol-scan` /
  `AssessConfig.eol_scan` opts out. Findings land in the evidence block,
  the report appendix, `--json` (`eol_scan`), and the report footer now
  states which mode (live vs. model-knowledge) produced the EOL claims
  alongside the existing OSV.dev CVE note.
- **`rebuild` is a first-class classification**: the recommendation phase's
  fixed vocabulary now distinguishes an incremental `strangler-rewrite`
  from a big-bang `rebuild` (build a replacement from scratch; the old
  system is a requirements document, not a foundation), and the prompt
  defines every option so the choice is deliberate.
- **Opt-in build probe** (`--build-probe` / `AssessConfig.build_probe`):
  the detected profile's setup/verify commands are actually executed —
  exit codes and output tails feed the buildability auditor as ground
  truth and land in the report appendix and `--json` (`build_probe`).
  Commands stop at the first failure; profiles with no locally runnable
  commands (legacy .NET Framework) skip with a recorded reason. Off by
  default: it runs the repository's own build (arbitrary code) and
  mutates the working tree the way any build does.
- **Lockfile parsing for the OSV scan**: exact resolved versions from
  `package-lock.json` (v1–v3), `poetry.lock`, `Cargo.lock` (workspace
  crates skipped), and NuGet `packages.lock.json` (`Project` references
  skipped) join the manifest pins, so range-specified projects still get
  a live vulnerability scan instead of the model-knowledge fallback.
- **Audit blind spots**: the report appendix and `--json` (`blind_spots`)
  deterministically name the top-level directories no phase finding (nor
  dead-code probe) cited — a sampled audit can no longer read as a
  complete one. Evidence citations count whether agents return a string
  or a list of paths.
- **Broken citation detection**: the report appendix and `--json`
  (`broken_citations`) deterministically flag findings that cite a bare
  file path (`Web.config`, `src/Api/Program.cs:42`) not present in the
  repository — the opposite failure mode from blind spots, and the $0
  automatic counterpart to the `--verify` re-check's "a citation that
  doesn't exist is itself a result." No agent call, no filesystem read of
  the cited path — pure set-membership against the already-enumerated file
  list. Prose and multi-part citations are deliberately left unflagged to
  keep the heuristic false-positive-free.

### Documentation
- **Invalid-JSON detection in shipped docs** (`doc_claim_issues`,
  `techwriter.py`): fenced `json` blocks are now parsed with stdlib
  `json.loads`, and a malformed example (trailing comma, unbalanced brace,
  etc.) is surfaced as an advisory finding naming the doc's path — the JSON
  half of #48's "bash/JSON" growth path, completing it alongside the
  bash-fence CLI-flag check below. `json.loads` only ever parses; unlike
  `pickle`/`yaml.load` it has no code-execution surface, so this is a
  strictly lower-risk check than the Python-fence `ast.parse` check #48
  already shipped. Advisory only, same `Documentation.unverified_claims`
  surface; an unterminated fence is skipped exactly like the Python/shell
  branches.
- **Hallucinated CLI-flag detection in shipped docs** (`doc_claim_issues`,
  `techwriter.py`): fenced `bash`/`sh`/`shell`/`console`/`zsh` blocks are
  now scanned line-by-line for `dev-team`/`python -m dev_team` invocations,
  and any `--flag` they cite is checked against the live
  `cli.build_parser()` option strings (deferred import, avoiding the
  `cli` → `engine` → `techwriter` cycle) — the bash-fence CLI-flag check
  named as the next increment in #48's own "Grows into" section. Advisory
  only, same `Documentation.unverified_claims` surface #48 already wired
  up; the shell text is regex-scanned only, never passed to `subprocess`,
  `os.system`, `os.popen`, `eval`, or `exec`.
- **`docs/TROUBLESHOOTING.md`**: a symptom-first operator runbook
  consolidating operational knowledge previously scattered across
  `DEPLOYMENT.md`, `docs/DISPATCH.md`, `docs/DASHBOARD.md`, and
  `docs/PIPELINE.md` — the "401 Invalid bearer token" env-file gotcha, the
  dispatch service's restart/queue-loss recovery, reading the access and
  event logs, a `needs-human` decision table across the six pipeline
  loops, and a dashboard/dispatch HTTP status quick-reference.
  `DEPLOYMENT.md`'s two env-file gotcha callouts now cross-link it. Docs
  only — no `src/` change, no new credential surface.
- **`docs/SECURITY.md`**: a consolidated security & threat-model reference
  mapping each threat area — prompt-injection handling, credential/token
  hygiene, execution containment, workspace/path containment, HTTP surface
  auth, and pipeline/CI guardrails — to the exact module/function that
  implements it, plus an explicit "what this does NOT protect against"
  section (the ROADMAP per-job isolation gap, the dashboard's
  unauthenticated-by-default localhost stance). Linked from `README.md`.
  A reference-checker test resolves every cited `dev_team.x.y` symbol
  against the installed package so the doc can't silently drift from the
  code. Docs only — no `src/` change, no new credential surface.

## [0.7.0] — Legacy-repo analysis: dead code, live CVEs, conventions, remote CI

### Assessment
- **Deterministic dead-code probes** feed the audit with exact, citable
  findings (no model guessing): `.cs` files no legacy MSBuild project
  compiles (`<Compile Include>` diff vs disk), `.csproj` files no `.sln`
  references, and top-level directories dormant for `dormancy_days`
  (default 365) while the repo stayed active (git-based; skipped cleanly
  outside git). Findings land in the evidence block, the report appendix,
  and `--json` (`dead_code`).
- **Live dependency vulnerability scanning via OSV.dev**: exact pins parsed
  deterministically from `packages.config` (NuGet), `package.json`,
  `requirements.txt`, and `Cargo.toml` are checked against the OSV batch
  API in one call — every ecosystem, one endpoint. Offline or failed
  queries degrade to a labelled model-knowledge fallback; `--no-osv-scan`
  opts out. The report footer now says which mode produced the CVE claims.
- **House-conventions capture**: a parallel side-phase profiles the
  repository's own style — naming, layout, test patterns, error handling —
  with citations, merges in machine-readable configs (`.editorconfig`,
  ReSharper `.DotSettings`, linter configs, rulesets), and persists to
  `.dev_team/conventions.json` (`--no-conventions` opts out). Advisory:
  its failure degrades the report, never the audit verdict.
- **Assessment → backlog bridge** (`--backlog` / `update_backlog`):
  remediation-plan steps, must-fix build blockers and dependencies,
  hardcoded secrets, dead-code hits, and live vulnerability records become
  estimated stories under one "Assessment remediation" epic in the
  persistent backlog, deduplicated by title so re-audits refresh instead
  of flood. This is the loop from "audited" to "remediated": delivery
  runs can now work the audit off story by story.
- **Exclude globs and component fan-out for monoliths**: vendored noise
  (`packages/`, `node_modules/`, `bin/`, `obj/`, binaries) is excluded
  from the tree, stats, and component detection by default (`--exclude`
  replaces the defaults; `--max-tree-entries` raises the cap), and
  `--component-fanout` gives each detected sub-project (per-directory
  manifests, `.csproj` included) its own parallel deep-dive section in
  the report, capped by `max_components`.

### Delivery
- **Legacy .NET Framework detection**: `packages.config` anywhere, or
  old-style project XML (`ToolsVersion`/`TargetFrameworkVersion`),
  resolves to a `dotnet-framework` profile that is *not locally runnable*
  — `dotnet test` never was going to work — instead of a profile that
  fails every task.
- **Graceful verification degrade**: on a stack with no runnable local
  verify command, gates degrade to an explicit `verification-unavailable`
  marker: review, security, and static findings become the quality bar,
  the fail-to-pass check is disabled as meaningless, and the events say
  exactly what happened and how to do better.
- **Remote CI verification gate** (`--remote-verify-status` /
  `--remote-verify-trigger`): delegate the Definition of Done to the CI
  system that *can* build the repo — trigger a run, poll a status command
  until it exits zero. Any CI with a CLI plugs in; polling cadence is
  configurable (`remote_verify_max_polls`, `remote_verify_interval_seconds`).
- **Conventions-aware engineer and reviewer**: a stored conventions
  profile is injected into implementation prompts ("match the house
  style") and review prompts (deviations are findings), so work on a
  legacy repo lands in the repo's own idiom instead of a modernisation
  patchwork.

### Assessment & .NET
- **A third engine audits existing repositories** (`--assess` /
  `DevTeam.assess`): read-only by construction (no branch, no gates, no
  commits, no bookkeeping; auditors get `Read`/`Grep`/`Glob` rooted at the
  workspace), five phases across the cast — inventory (architect),
  buildability without running installs (DevOps), dependency/secret/data/
  external-service risk (security), test & doc reality (QA), and a fixed-
  vocabulary classification (revive-in-place / dependency-surgery /
  strangler-rewrite / archive) with a sequenced remediation plan and the
  single highest-risk item (product manager). Deep phases run in parallel;
  every claim carries a file-path citation; a deterministic LOC/extension
  inventory anchors the prompts; failed phases degrade into the report
  instead of unwinding the run; `--interactive` adds a post-inventory scope
  pause (narrow or abort). Output is one cited markdown report (default
  `audit/assessment.md`, the run's only write) or `--json`. See
  `docs/ASSESSMENT.md`.
- **.NET support**: root `.sln`/`.csproj`/`global.json` resolve to a
  `dotnet` profile (`dotnet test`/`restore`, vulnerable-package scan) that
  wins over `package.json` so full-stack monoliths resolve to their
  solution; repo context reads .NET manifests; baseline attribution parses
  VSTest and xUnit failure output.

### Interactivity & personas

#### Interactive runs
- **An `InteractionChannel` puts a human in the loop**: `--interactive` runs
  pause for plan review (approve / revise with feedback / abort) before any
  task work, escalate a task that exhausted its attempts (skip, or retry
  with guidance fed to the engineer as review feedback), and route the
  feature commit and policy-gated commands (`push`/`deploy`/`rm`) through
  interactive approval. Every question's default answer preserves the
  autonomous behaviour, EOF degrades to it, and resumed checkpoints don't
  re-litigate an already-approved plan. `QueueChannel` (thread-serviced,
  with timeout fallback) is the integration surface for external UIs;
  `ScriptedChannel` is the test double. See `docs/INTERACTION.md`.
- **`--chat` opens a conversation with the product manager** on a persistent
  `ClaudeSDKClient` session (context retained across turns) to shape the
  feature request before any run; `/run` / `/deliver` distil the
  conversation into the brief and hand it to the team, returning to the
  chat afterwards.

#### Personas
- **Every agent has a name**: a default cast (Priya, Anders, Sam, Rey,
  Quinn, Sasha, Wren, Riley, Devon) is injected additively into system
  prompts and carried on progress events (`[Priya (product-manager)/...]`).
  `--roster FILE` overlays custom names/styles (unknown roles rejected);
  `--no-personas` disables. Names are presentation only — checkpoints,
  memory, and events stay keyed by role.

### Review hardening

#### CLI & deployment
- **Claude subscription support is first-class**: the CLI now preflights
  credentials and accepts `CLAUDE_CODE_OAUTH_TOKEN` (a Pro/Max/Team/Enterprise
  subscription token from `claude setup-token`) alongside
  `ANTHROPIC_API_KEY`, a stored `claude` login, or gateway/Bedrock/Vertex
  variables — a missing credential now fails fast with guidance instead of
  surfacing as an opaque CLI error mid-run. `DEPLOYMENT.md` is now a full
  Ubuntu server install guide covering both auth options, and the systemd
  unit and Docker examples document the subscription token.
- **`DEPLOYMENT.md` hardening from a real bare-metal install**: prerequisites
  now include `python-is-python3` (a minimal Ubuntu server ships only
  `python3`, but auto-detected verify/gate commands and agent-authored tests
  call bare `python`, so a task is wrongly reported failed without it), and the
  systemd section warns that `EnvironmentFile` — unlike a shell `source` — does
  not strip an inline `#` comment, so a `KEY=value  # note` on the credential
  line corrupts the token into a confusing `401 Invalid bearer token`.

#### Delivery engine
- **Accepted work is banked**: every task that passes its gates is committed
  as a `wip(dev-team)` commit on the delivery branch, so a later task's
  rollback (hard reset) can no longer destroy earlier gated work, per-task
  diffs/reviews are no longer contaminated by prior tasks' changes, and the
  final feature commit is a soft-reset squash of the banked work.
- **Resume actually works**: checkpoints are per-feature files, carry the
  run's plan (reused on resume instead of gambling on a regenerated plan
  matching) and the original baseline sha (so the final squash spans the
  interrupted run's work). Corrupt checkpoint/memory/backlog files read as
  empty instead of crashing; `LocalWorkspace` writes are atomic
  (write-then-rename).
- `.dev_team/` is appended to an existing `.gitignore` (previously only
  written when none existed), so rollbacks no longer delete the engine's own
  checkpoint mid-run and leftovers no longer read as a dirty tree.
- Conflicted squash-merges in worktree mode are cleaned up and fed back to
  the engineer instead of poisoning the delivery branch for all later tasks;
  stale worktrees/branches from crashed runs are pruned and force-reset;
  `git stash` push/pop pairs are serialised across worktrees (the stash
  stack is repo-global).
- Non-agentic deliveries to a real directory get the same safeguards as
  agentic ones (dirty-tree halt, delivery branch, baseline commit); a
  workspace nested inside a larger repo no longer silently adopts the
  enclosing repo; git commands carry a timeout; duplicate task ids are
  renamed instead of crashing the scheduler after agent spend; planning or
  specialist-stage failures return a halted/partial outcome instead of
  unwinding the run.
- Security review evidence is reconciled against git so resumed tasks' files
  cannot be committed unseen.

#### Governance & measurement
- The approval gate is consulted before the feature commit; approval-token
  matching is by command position (`git push` gates, `git stash push` does
  not); a denied fail-to-pass stash is reported instead of silently skipped.
- A real workspace gets a persistent backlog by default, and reruns update
  the existing epic/stories instead of duplicating them per run.
- Project memory merges across runs (bounded) instead of each run erasing
  the last; ADR numbering continues across runs.
- The per-run scorecard is part of `DeliveryOutcome`, `--json` output, and
  the rendered summary.
- Evals: `max_cost_usd` makes cost part of a case's score; behavioural
  `check_commands` fail honestly on dry-run workspaces instead of vacuously
  passing; trace spans are closed when an agent call raises.

#### Agents & model I/O
- `extract_json` prefers the last JSON object in output (mid-task narration
  can no longer hijack the answer); non-object roots are rejected and
  retried; unknown severity strings fail **closed** (high/blocker block
  instead of downgrading to info).
- Non-engineer agents run with an explicit read-only tool allowlist rooted
  at the workspace (previously: unrestricted tools in the orchestrator's own
  cwd with edits auto-accepted).
- SDK calls carry a timeout and transient SDK errors are retried; untrusted
  content (file bodies, diffs, scanner output, prior-run memory) is fenced
  and marked as data-not-instructions in prompts; omitted/truncated review
  evidence is labelled; the JSON-retry prompt is self-contained.

#### Packaging & tooling
- Added a `LICENSE` file (MIT), `CHANGELOG.md`, and a `py.typed` marker.
- CLI: `--version` flag; deliver-only flags are rejected without `--deliver`
  (exit code 2); errors and `--verbose` progress go to stderr so `--json`
  output stays pipeable.
- Real-git integration tests for the `GitRepo` porcelain (which caught a
  real `rev-parse` misparse: failures echoed the ref name instead of "").
- Ruff lint gate (`make lint`, CI step); CI matrix extended to Python 3.13;
  git added to the container image and deployment prerequisites.

## [0.6.0]

Benchmark-grounded agents — fail-to-pass QA, SAST triage, budgeted review,
ADR-consistent design, INVEST plans, artifact-shipping specialists.

## [0.5.0]

Brownfield depth and parallel scale — repo context, baseline attribution,
per-task worktrees, retrospectives.

## [0.4.0]

Behave like a professional in real repos — baseline gates, delivery branches,
curated commits, diff-defined review.

## [0.3.0]

Make delivery real — agentic engineer, evidence-based review,
workspace-rooted gates.

## [0.2.0]

Real delivery engine: research-driven multi-agent capabilities.

## [0.1.0]

Initial multi-agent development team on the Claude Agent SDK; CI with a
least-privilege, matrixed, concurrency-controlled workflow.
