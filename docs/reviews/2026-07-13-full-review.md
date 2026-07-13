# Full repository review — security, usability, outputs, and the path to 100% of the vision

*Date: 2026-07-13 · main @ `7242909` · reviewed by four parallel adversarial passes
(security / engine correctness / usability & outputs / vision gap), each verified
against source before inclusion. All file:line references are to main @ `7242909`.*

## Executive summary

dev-team is in unusually good shape for a system of this ambition. The test suite
is genuinely green at **100% line and branch coverage** (1114 tests, real
behavioural assertions, only 5 justified `pragma: no cover` sites). The security
posture is largely *implemented*, not aspirational: token hygiene, argv-semantic
command policy, escape-first rendering, constant-time auth, and the pipeline
workflows are model-grade hardening. **No critical or high-severity security
vulnerability was confirmed.**

The material findings cluster in three places:

1. **Git process-state edge cases in the engine** — one high-severity correctness
   bug (unreviewed edits can be banked after an engineer exception) and two
   medium ones (silent stash loss, final-commit reset ordering) where a
   best-effort git call lets the "only gated work accumulates" invariant leak.
2. **Dispatch operability** — failed jobs report `cost_usd: 0`, hung jobs wedge
   the single-flight worker forever, and the in-memory queue has no crash story.
3. **Onboarding & output surface** — the dashboard tells operators to run a
   command that does not exist, there are no sample outputs anywhere in the
   docs, and the assessment report omits the finding ids that two shipped
   features key on.

Against the vision, the system is roughly **70–75% complete**. The two absent
pillars are **container-level sandboxing** (the trust boundary and the isolation
boundary disagree — the system's day job is cloning untrusted repos) and a
**PR delivery target** (the product's delivery terminus is a local commit;
zero push/PR code exists in `src/`). The self-improvement pipeline itself is
~85% and demonstrably closes the loop (issues #33/#35/#37 → PRs #34/#36/#38,
human-merged), but nothing yet *measures* whether the product improves.

---

## Part 1 — Security

### Verdict

No confirmed critical/high vulnerabilities. Five residual findings, all
hardening items on top of controls that already work.

### Findings

**S1 (MEDIUM) — The approval gate is a no-op by default.**
`engine.py:454` defaults to `AutoApprover()` (`approval.py:41-45`), so the
`SideEffectPolicy` verdict `requires_approval` (for `push`/`deploy`/`rm`,
`policy.py:277-283`) is auto-granted. The gate is an audit hook, not a
human-in-the-loop stop — in tension with CLAUDE.md §1 for unattended runs.
*Fix:* default unattended deployments to `PolicyApprovalGate(block_risks=("high",))`
(the policy already tags these `risk="high"`), or make a non-auto gate the
engine default with AutoApprover opt-in.

**S2 (LOW/MEDIUM) — Command policy is denylist-only by default.**
`policy.py:210` ships an empty `allowed_programs`, so anything outside the small
structural denylist (`rm -rf`, `sudo`, `mkfs*`, fork bomb) runs. The argv-semantic
matching itself is excellent; the default posture is the gap. *Fix:* populated
allowlists for untrusted tiers — and ultimately containment (see Part 4, item 1).

**S3 (LOW) — `parse_repo` accepts arbitrary URL schemes including `file://`.**
`sources.py:107-133`, reachable from the authenticated dispatch API
(`dispatch.py:184`, `build_spec` at `dispatch.py:302-308`). A token-holder can
clone any local on-disk git repo into a job workspace and browse its
report/transcripts via the dashboard. Bounded impact (authenticated, tailnet,
git-repos-only) but an unnecessary local-filesystem reach. *Fix:* allowlist
`https`/`ssh` schemes; `file://` only behind an explicit test flag.

**S4 (LOW/MEDIUM) — Per-job runtime isolation relies on systemd hardening only.**
`deploy/dev-team-dispatch.service:42-52` has a good baseline
(`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`) but no
`SystemCallFilter`, `RestrictNamespaces`, `CapabilityBoundingSet=`, or egress
control, and no per-job boundary — one job's executed code can read another
job's workspace. *Fix:* seccomp + namespace restrictions now; per-job rootless
container with no outbound network for the assess path as the real fix.

**S5 (LOW) — The prompt-injection fence is instructional, not structural.**
Untrusted README/manifest text is interpolated between guessable tags
(`context.py:70`, `assessment.py:1553-1555`); a literal `</manifest-content>`
in a hostile README can attempt a fence break. Mitigated by the standing
`UNTRUSTED_CONTENT_NOTE` (`agents/base.py:20-26`) and by the fact that most
untrusted content reaches agents via Read/Grep rather than interpolation.
*Fix:* neutralise fence tokens in interpolated content, or use a per-run nonce
in the tag name.

### Controls worth crediting (verified, not vibes)

- **Workflows:** `persist-credentials: false` everywhere an agent runs;
  per-step `GH_TOKEN` on deterministic steps only; fork PRs structurally
  receive no secrets; "humans merge" enforced by `--allowedTools` (no
  `gh pr merge`, no `gh api`, exact-match `Bash(git push origin HEAD)` —
  no refspec/force holes) with branch protection as backstop; two-hop
  dispatches carry PR numbers only and re-verify eligibility from the API
  (`pipeline-pr-conflict.yml:264-298`); untrusted text never interpolated
  into `run:` blocks; actions pinned by full SHA; attempt caps prevent
  token-drain loops.
- **`execution.py`:** `SECRET_ENV_KEYS` scrubbed from every child env;
  path normalisation rejects absolutes and `..`; realpath+commonpath
  symlink-escape check; atomic write-then-rename.
- **`sources.py`:** textbook git token hygiene — per-command `GIT_CONFIG_*`
  `http.extraheader` (never argv/URL/`.git/config`), GitHub-HTTPS-only
  attachment, token popped from `os.environ`, embedded-credential URLs
  rejected, token and derived base64 header scrubbed from error output.
- **HTTP surfaces:** constant-time bearer compare on both services; dispatch
  hard-fails without a token; `Content-Length` validation with 1 MiB cap;
  workspace-membership traversal guards on report/transcript serving;
  `HttpOnly; SameSite=Strict` session cookie; dispatch token never reaches the
  browser; escape-first markdown rendering with `^https?://` link validation.
- **Prompt injection:** `UNTRUSTED_CONTENT_NOTE` on every agent; assess/verify
  agents restricted to read-only tools, so auditing an untrusted repo never
  executes its code.

---

## Part 2 — Engine correctness & robustness

Test suite verified green in a clean venv: 1114 passed, 100% line + branch
coverage, gate real in `pyproject.toml` (`--cov-branch`, `--cov-fail-under=100`,
`filterwarnings = ["error"]`). Coverage is earned, not padded. The ruff gate is
deliberately minimal (`select = ["F","E9"]`).

### Findings

**E1 (HIGH) — Engineer exception in agentic (non-worktree) mode leaks unreviewed
edits into the next task's WIP commit.**
`engine.py:1169-1181`: `implement_in_place` mutates the shared workdir but sits
*outside* any rollback scope — `_integrate`'s `except Exception: self._rollback(...)`
(engine.py:1468-1472) covers only review/QA/gates. If the engineer raises
(`AgentResponseError` on unparseable prose, or the budget raising after
recording — see E6), the dirty tree survives; the next task's `_commit_wip`
(engine.py:1640-1646) stages **all** non-internal changed files, banking the
failed task's unreviewed edits under the wrong task — violating the module's
core invariant. With no further task, resume either halts on the dirty-baseline
check or (with `allow_dirty_baseline=True`) sweeps the edits into the baseline.
*Fix:* wrap the engineer call so any exception triggers `self._rollback(None, repo)`
before re-raising.

**E2 (MEDIUM-HIGH) — Best-effort `stash_pop` can silently lose a gated
implementation and still mark the task DONE.**
`git.py:191-194` (`check=False`, result ignored), used by fail-to-pass at
`engine.py:1518-1534`. A conflicting pop (verify command rewrote the same paths)
leaves the implementation in the stash; the reverted tree is then committed
(`allow_empty=True` masks it) and the checkpoint marks the task done — a resume
never redoes it. Verified related edge: `git stash push -u -- <paths>` exits 0
with "No local changes to save" when the pathspec has no diff, so `stash_push`
returns True without creating an entry and the later pop pops an unrelated
pre-existing user stash. *Fix:* `check=True` (or verify) the pop on this path;
have `stash_push` confirm an entry was actually created.

**E3 (MEDIUM) — Final-commit failure destroys banked WIP commits and bricks resume.**
`engine.py:1852-1878`: `_commit_if_approved` does `reset_soft(baseline)` *then*
`commit(...)`. If the commit fails (hook, GPG signing), the branch tip has
already moved: every `wip(dev-team)` commit is reflog-only, the tree is staged
but uncommitted, and the next run halts. *Fix:* commit first, reset only after
success (or trial-commit before resetting).

**E4 (MEDIUM) — Failed dispatch jobs report `cost_usd: 0.0`.**
`dispatch.py:467-472` hard-codes it on the exception path; `result()`
(dispatch.py:1119-1124) serves it. A deliver job that burns $40 of a $50 budget
and dies reports zero — undercounting exactly the expensive runs, on a shared
pool. The `Budget` in `run_job` knows the true spend; `verify_finding` already
gets this right (assessment.py:1621-1629). *Fix:* meter `budget.spent` into the
failure record.

**E5 (MEDIUM) — No stuck-job handling; a hung job wedges the service; restart
drops queued jobs silently.**
`dispatch.py:269-280, 458-479`: `run_job` has no timeout — one hung SDK call
blocks the single-flight worker forever; later jobs queue until the 16-slot cap
turns submits into 503s; no watchdog, no cancel API. The registry is
memory-only: on restart queued jobs vanish and running jobs 404 with no terminal
record. `jobs_root` clones are never garbage-collected (archive only flips
`meta.json`), so disk grows without bound. *Fix:* per-job timeout + cancel
endpoint; persist a minimal job record beside `meta.json`; a GC/retention policy.

**E6 (MEDIUM-LOW) — Budget record-then-raise discards a paid-for result and
skips its transcript.**
`instrument.py:73-78` / `budget.py:130-141`: the raise fires after recording,
so the completed, paid result is thrown away and the transcript recorder never
runs for that call — an audit gap on exactly the boundary call, and the trigger
for E1 in agentic mode.

**E7 (LOW-MEDIUM) — Baseline gate and `RemoteCIGate` block the event loop.**
`engine.py:592, 1002` call the runner synchronously (task-time gates correctly
use `asyncio.to_thread`); up to 1800s of loop blockage — interaction channels
and event listeners starve. `verification.py:164-171` also `time.sleep`s.

**E8 (LOW) — The reviewer diff omits newly created files.**
`git.py:80-83` uses `git diff HEAD`, which excludes untracked files, so in
agentic mode the reviewer's `diff` misses every file the engineer *created*.
Mitigated by `_merge_unreported_changes` + the contents dict, but "the diff
defines the change" is not quite true.

**E9 (LOW) — Model-authored task ids flow unsanitised into worktree paths and
branch names.** `engine.py:1230-1231`; ids come from LLM JSON
(`parsing.py:133-148`) with no character validation. `/`, `..`, or spaces break
worktree/branch creation (contained via `GitError`) and are a mild path-escape
surface. *Fix:* slug-validate ids at parse time.

### What's solid

Fail-closed test attribution (`failures.py` returns `None`, never an empty set,
for unrecognised output and refuses attribution when either side is unknown);
verdict calibration validates at write *and* read time and never promotes
out-of-contract verdicts; checkpoint/resume matches tasks on id + content
fingerprint and fails toward redoing work; the scheduler contains per-task
exceptions and detects cycles; every side effect sits behind a small injected
protocol, which is why 100% branch coverage is achievable offline and honest.

---

## Part 3 — Usability & outputs

### Findings (ranked by operator impact)

**U1 — The dashboard tells operators to run a command that does not exist.**
`dashboard.py:1764` renders a copyable `dev_team_verify <source_job> <finding_id>`
(also docs/DASHBOARD.md:96; even asserted in `tests/test_dashboard.py:252`), but
the only console script is `dev-team` (pyproject.toml:39-40). The flagship
verification loop dead-ends at "command not found". *Fix:* render the real
invocation (dispatch `mode:"verify"` curl, or `dev-team --verify <dir>
--finding '<id>'`) or ship a matching console script.

**U2 — A mistyped `--assess --workspace` silently runs a paid audit of an empty
directory.** `LocalWorkspace.__init__` mkdirs its root (`execution.py:140`);
`_validate_args` guards `--dashboard`/`--verify`/`--make-backlog` against
missing directories (`cli.py:569-577`) but not `--assess`/`--deliver`, and the
assessment engine has no zero-file guard. *Fix:* extend the exists/non-empty
check; abort before phase 1 when `inventory_stats().total_files == 0`.

**U3 — docs/INTERACTION.md documents the opposite of the EOF fail-safe.**
INTERACTION.md:44-47 says a closed stdin "degrades to autonomous"; the code
deliberately fails closed — plan review → `abort` (`interaction.py:284-303`),
approvals → `deny`. The code is right; the doc is stale on a safety-relevant
expectation. *Fix:* correct the doc.

**U4 — One flat namespace of 45+ flags; modes are flags; `--help` is a ~180-line
wall.** Seven mutually-exclusive modes live beside ~20 mode-scoped flags whose
scoping exists only as "(with --X)" prose (`cli.py:99-457`). The validation
layer proves the modes are disjoint — the parser just doesn't express it. Also
`--workspace`'s help says "(with --deliver)" (`cli.py:336-339`) though it's
equally the `--assess`/`--dashboard` target. *Fix:* argparse subcommands, or at
minimum argument groups per mode.

**U5 — The default mode is a paid run.** `dev-team "Title" "Desc"` immediately
runs the paid simulation with no notice; the "not free" warning lives on the
`--deliver` flag's help text. *Fix:* one-line stderr notice at simulation start;
consider a budget prompt.

**U6 — Failed jobs report `cost_usd: 0` to Dave/the operator** (same defect as
E4; DISPATCH.md:148 faithfully documents the wrong number).

**U7 — The markdown report never shows finding ids, but two features key on
them.** `--verify --finding` and dispatch `mode:"verify"` address findings as
`risk.secrets[0]` (cli.py:313-320, DISPATCH.md:249-264), but `render_report`
(assessment.py:1752-1932) prints anonymous bullets — a repo owner reading
`audit/assessment.md` cannot name the finding to re-check. *Fix:* suffix each
bullet with its positional id from the same enumeration `list_findings` uses.

**U8 — Ctrl-C during a paid run dumps a raw asyncio traceback.** `main()`
catches only `DevTeamError`/`ValueError` (cli.py:1171-1175); dashboard/dispatch
handle `KeyboardInterrupt` but deliver/assess/chat/simulation do not — no word
about spend so far or the resume checkpoint. *Fix:* catch in `main()`, print
"interrupted — $X.XX spent; checkpoints allow resume", exit 130.

**U9 — No sample outputs anywhere.** No example assessment report, no delivery
summary, no dashboard screenshot; README is 130 lines of checkmarks before the
first usage example. For a product whose deliverable *is* a report, this is the
biggest onboarding gap. *Fix:* commit `docs/examples/assessment-sample.md` from
a real redacted run, a dashboard screenshot, and a "first 10 minutes" README
section (install → token → assess a small repo → open dashboard).

**U10 — Dispatch API: no version surface; restart-split semantics; no
pagination.** No `/v1` prefix (version only informational in `/health`); after
a restart `GET /jobs/{id}` 404s while `/findings`/`/backlog` still answer from
disk — a polling client sees a job vanish mid-flight; `GET /jobs` hard-capped
at 25 (dispatch.py:106). *Fix:* persist minimal job state (ties into E5),
`?limit=`/cursor, and a stated compatibility policy in DISPATCH.md.

**U11 — Chat spends without showing the brief first.** `/run`//`/deliver`
distil and immediately execute (chat.py:138-149); no `/brief` preview, no
confirmation, no accumulated-cost display. *Fix:* print the distilled brief and
confirm before `run_feature`.

**U12 — No systemd unit or DEPLOYMENT.md coverage for the standing
dispatch+dashboard pair** — the deployment the whole Dave workflow assumes must
be reverse-engineered from `dev-team-dispatch.service`. *Fix:* add
`deploy/dev-team-dashboard.service` + a "standing services" section.

**U13 — Minor drift:** README:75 says `rebuild-from-scratch`, the closed
vocabulary is `rebuild` (assessment.py:120-124); exit-code semantics live only
in README and a docstring, not `--help`; `report.py:133` truncates failed-gate
detail to 200 chars with no pointer to the full output in events.jsonl.

### What's genuinely good

Credential first-run failure is exemplary (three copy-pasteable remedies, exit
2, before any spend — cli.py:69-77); ~25 flag-combination validations with
educational messages; strict stdout/stderr discipline with `--json` everywhere;
DISPATCH.md is exceptionally accurate against the code; the assessment report
is honest by design (blind spots listed as unexamined, cost footer separating
live OSV scan from model recollection, failed phases rendered as failures);
EOF fails closed; the Kanban board is real (editable cards, dependency cycle
rejection, per-story finding provenance, calibration panel).

---

## Part 4 — Vision gap and the path to 100%

### Pillar completeness (verified against code)

| Pillar | ~% | The gap |
|---|---|---|
| Trustworthy assessments | 85 | Verification is one-finding-at-a-time; calibration is displayed but feeds nothing back; EOL judgments remain model knowledge |
| Professional delivery | 75 | Terminus is a **local commit** — zero push/PR code in `src/`; ROADMAP 2/3/4/5 all land here |
| Operability (dashboard+dispatch) | 80 | No interactive question surface (`QueueChannel` seam exists, unserved); dashboard auth opt-in/open by default; single-flight ceiling |
| Budget / cost story | 70 | No per-role token budgets; per-run cost evaporates (no history/trending); pool coordination is prose |
| Security posture | 65–70 | **Containment** — ROADMAP 1, no container code exists; transcripts (the audit log) off by default vs CLAUDE.md §6 |
| Self-improvement pipeline | 85 | Demonstrably closes the loop (#33/#35/#37 → PRs #34/#36/#38, human-merged) but nothing measures product improvement |

**Overall: roughly 70–75%.**

### ROADMAP items in code

| # | Item | Status |
|---|---|---|
| 1 | Container sandboxing | **Absent** (policy.py + doc warnings are the only mitigation) |
| 2 | PR / CI integration | **Absent** as delivery target; `remote_verify_*` (engine.py:187-190) covers only the CI-as-gate corner |
| 3 | Dynamic re-planning | **Absent** |
| 4 | Retrieval + context budgeting | **Absent** |
| 5 | Session continuity across attempts | **Absent** (ClaudeSDKClient used only by chat, sdk.py:201-230) |
| 6 | LLM retrospectives + benchmark history | **Partial ~30%** (deterministic retros + on-demand evals; no score persistence, no nightly run) |
| 7 | Richer interaction surfaces | **Partial ~40%** (QueueChannel seam shipped; nothing serves it) |
| 8 | MCP tools + group review | **Absent** |

### The ordered path to 100%

**Phase 0 — fix what's found (this review; mostly one-PR slices):**

0a. **E1+E6** — rollback scope around the agentic engineer call; the invariant
    the whole engine is named for. 0b. **E2** — verified stash push/pop on the
    fail-to-pass path. 0c. **E3** — commit-then-reset ordering in
    `_commit_if_approved`. 0d. **E4/U6** — real `cost_usd` on failed jobs.
    0e. **U1** — fix the fictional `dev_team_verify` command. 0f. **U2, U3, U8,
    U13** — cheap guard + doc corrections. Each is small, evidence-backed, and
    maps cleanly to pipeline proposals with testable acceptance criteria.

**Phase 1 — the two absent pillars:**

1. **Container-level sandboxing** (ROADMAP 1, `theme:security`) — the one place
   the trust boundary and isolation boundary disagree; every other feature
   inherits the risk. Multiple PRs: (a) container-backed `CommandRunner`
   (rootless, no network, workspace bind-mount) for gates/probes; (b) the
   engineer's tool loop in the same container; (c) dispatch/deploy wiring.
   Subsumes S2/S4.
2. **PR delivery target** (ROADMAP 2, `theme:dispatch`) — "deliver changes
   professionally" means a PR. 1–2 PRs: push `dev-team/<feature>` + open PR
   with the outcome report as body (token plumbing in `sources.py` already
   exists), then check-watching feeding CI failures into the existing
   gate-feedback loop.

**Phase 2 — make improvement measurable, then cheap:**

3. **Benchmark score history + nightly budget-capped eval run** (ROADMAP 6,
   `theme:verification`) — without a score trail, "the pipeline makes the
   product better" is unfalsifiable. 2 small PRs: persist `EvalReport` as
   append-only JSONL keyed by commit; a scheduled capped workflow surfacing
   deltas.
4. **Dashboard question surface** (ROADMAP 7, `theme:dashboard`) — headless
   deployment currently forfeits the entire `--interactive` value; the seam is
   built, this is an adapter. 1 PR: pending-question endpoint + choice buttons,
   wired through dispatch.
5. **Session continuity across engineer attempts** (ROADMAP 5,
   `theme:efficiency`) — the single biggest known token waste on the shared
   pool; transport proven by chat. 1 PR.

**Phase 3 — delivery depth:**

6. **Dynamic re-planning** (ROADMAP 3) — bounded replace/split on final-attempt
   failure, reusing the interactive question vocabulary so item 4's surface
   supervises it. 1–2 PRs.
7. **Retrieval + context budgeting** (ROADMAP 4) — correct but the least
   one-PR-able; do after 5 (which cuts cost more cheaply). Multiple PRs.
8. **MCP tools + group review** (ROADMAP 8) — lowest leverage; needs the
   CLAUDE.md §7 allowlist/registration story anyway.

**Cross-cutting one-PR slices the ROADMAP doesn't list (ideal pipeline
proposals):**

9. **Always-on redacted audit log** of agent I/O metadata (role, prompt hash,
   cost, verdict — no raw repo content) — closes the CLAUDE.md §6 "log prompts
   and responses" tension without the data-sensitivity cost (`theme:security`).
10. **Calibration feedback loop** — `confirm_rate` per phase is computed and
    displayed but consumed by nothing; feed low-confirm phases back into their
    prompts or flag them low-trust in reports (`theme:verification`).
11. **Cost ledger + dashboard tile** — per-run `cost_usd` exists everywhere and
    evaporates; append-only history makes "every run has a cost story"
    verifiable and gives item 3 its cost axis (`theme:efficiency`).
12. **Dispatch robustness** — job timeout + cancel + persisted job state +
    GC/retention (E5/U10) (`theme:dispatch`).
13. **Secure-by-default dashboard** — flip non-local unauthenticated binds from
    warn to refuse; IdP later (`theme:security`).
14. **Sample outputs + first-10-minutes onboarding** (U9, U12)
    (`theme:docs`).
15. **Real-world usage evidence** — not a PR: the repo contains no assessment
    artifacts, case studies, or `operator-feedback` issues; the VISION's
    evidence-driven proposal loop starves without them. Run real audits, file
    what breaks.

**Sequencing rationale:** Phase 0 protects the invariants everything else
stands on; item 1 unblocks safe unattended scale; item 2 completes the
mission's delivery interface; item 3 makes the self-improvement claim
measurable (and everything after it provable); 4–5 are cheap, high-leverage
adapters on existing seams; the rest is the long tail. Items 9–14 map cleanly
to VISION theme labels with obvious 100%-coverage acceptance criteria — feed
them to the research loop as evidence.
