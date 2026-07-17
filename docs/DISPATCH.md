# The dispatch service (`--dispatch`)

An authenticated HTTP API that lets an external caller drive the team
remotely: **submit** an assess, deliver, or verify job against a repository,
**poll** its status, and **fetch** the result. It wraps the same code paths
as `dev-team --assess` / `--deliver` / `--verify` — clone the repo, build a
`DevTeam` (or, for verify, one fresh skeptical agent), run it.

```bash
DEV_TEAM_DISPATCH_TOKEN=$(openssl rand -hex 32) \
  dev-team --dispatch --host 127.0.0.1 --port 8738
# dev-team dispatch service at http://127.0.0.1:8738/ (Ctrl-C to stop)
```

The service runs real agents, so it needs Claude credentials in the
environment (the same preflight as any run). It is a **standalone process** —
not combined with `--assess`/`--deliver`/`--chat`/`--dashboard`.

## Auth

Every route **except `GET /health`** requires a bearer token:

```
Authorization: Bearer <token>
```

The token is read from the `DEV_TEAM_DISPATCH_TOKEN` environment variable at
startup (a missing/empty token is a hard error — the service never runs
unauthenticated). It is compared with `hmac.compare_digest` (constant-time);
a missing or wrong token returns `401 {"error":"unauthorized"}`.

The token is a credential: keep it out of version control, hand it only to the
authorised caller, and rotate it by editing the env file and restarting.

## Single-flight

Submitted jobs run **one at a time**. A background worker thread owns an
asyncio event loop and drains the job queue strictly sequentially: the box has
one shared Claude subscription and dev-team has no cross-run locking, so
overlapping runs would corrupt each other. A submit returns immediately with a
queue `position` (0 = starts as soon as the worker is free); the pending queue
is capped (default 16) and a submit past the cap returns `503`.

## Dashboard visibility

Dispatched jobs run in their own isolated workspaces (`<jobs-root>/<id>`), so
they don't appear on a standing `dev-team --dashboard` (which watches a single
workspace) by default. Pass `--dashboard-workspace DIR` to the dispatch service
and every job **also** journals its events into `DIR` under the same run id —
so it shows up as its own run/agent-cards on a dashboard pointed at `DIR` — and
an assess run mirrors its report to `DIR/audit/<job-id>/assessment.md` for the
Reports panel, **its structured result to
`DIR/audit/<job-id>/assessment.json`** (the exact `outcome_to_dict` shape),
and **its repo identity to `DIR/audit/<job-id>/meta.json`**
(`{"repo","mode","id"}`). That JSON is the disk-keyed record
`POST /jobs/{id}/backlog`, `GET /jobs/{id}/findings`, and the `verify` mode
read later — the in-memory job registry is lost on a service restart, but
the persisted assessments are not (`meta.json` is how a verify job knows
which repository to re-clone after a restart). Verify verdicts append to
`DIR/audit/<source-job-id>/verifications.jsonl`, which is what
`GET /jobs/{id}/verifications` reads. The job's own workspace stays the source of truth; this
is a read-only visibility copy (the shared backlog in `DIR` is the one
deliberate exception — see `backlog` below). Point the dashboard and the
dispatcher at the same `DIR` (e.g. `/opt/dev-team/workspace`) to watch
dispatched runs live.

## API

Base `http://<host>:<port>`. All bodies are JSON.

### `GET /health` (no auth)

```json
{"status":"ok","service":"dev-team-dispatch","version":"0.7.0"}
```

### `POST /jobs` (auth) — submit

Body (assess/deliver):

```json
{"mode":"assess|deliver","repo":"owner/name or url",
 "title":"...","description":"...","budget_usd":10,"backlog":false,
 "interactive":false,"interactive_timeout_seconds":300}
```

Body (verify — see *Finding re-verification* below):

```json
{"mode":"verify","source_job":"assess-…","finding_id":"risk.secrets[0]",
 "budget_usd":2}
```

- `mode` must be `assess`, `deliver`, or `verify`.
- `repo` is a GitHub `owner/name` slug or any git URL (must parse). Not
  sent for `verify` — the repo comes from the source job's `meta.json`.
- `budget_usd` is `null` or a positive number.
- `backlog` (optional, default `false`, must be a boolean): with `true` an
  assess job also converts its findings into stories — in the job's own
  `.dev_team/backlog.json` and, when a `--dashboard-workspace` is configured,
  merged into that workspace's shared backlog (deduplicated by title).
  Ignored for `deliver`. Leaving it `false` costs nothing later: backlog
  generation can always be run after the fact via `POST /jobs/{id}/backlog`.
- `interactive` (optional, default `false`, must be a boolean): with `true`
  on a `deliver` job, the run pauses for plan review, re-plan supervision,
  and failure escalation exactly like the CLI's `--interactive`, answerable
  over `GET`/`POST /jobs/{id}/question`/`answer` below — see *Interactive
  deliver*. Accepted but ignored on `assess`/`verify` (mirrors `backlog`
  above).
- `interactive_timeout_seconds` (optional, must be a number or `null`):
  meaningful only alongside `interactive: true`. Resolves to `300` when
  omitted, and is **clamped to `[30, 1800]`** before use — a request outside
  that range is silently bounded, never rejected or stored as-is, so a
  misconfigured or malicious huge timeout cannot wedge the single-flight
  worker on one paused job.
- `deliver` requires a non-empty `title` and `description`.
- `assess` defaults `title` to the repo slug and `description` to `""`.
- `verify` requires a non-empty `source_job` and `finding_id`; it is
  **validated synchronously against disk** at submit time (missing
  assessment/finding → immediate `404`, no queue slot wasted).

→ `202 {"id":"assess-…","state":"queued","position":0}`. Errors:
`400 {"error":…}` (bad mode/repo/budget, non-boolean backlog, non-boolean
`interactive`, non-numeric `interactive_timeout_seconds`, missing title or
description for deliver, missing source_job/finding_id for verify,
malformed JSON), `401`, `404` / `409` (verify — see below),
`503 {"error":"queue full"}`.

### `GET /jobs` (auth) — list

Newest-first, capped at 25. Archived jobs (see *Archive / unarchive* below)
are excluded by default; `?archived=1` includes them:

```json
{"jobs":[{"id":"…","mode":"assess","repo":"…","state":"…",
          "started":123.0,"ended":null}]}
```

### `GET /jobs/{id}` (auth) — status

```json
{"id":"…","mode":"assess","repo":"…",
 "state":"queued|running|succeeded|failed|cancelled",
 "started":num|null,"ended":num|null,"cost_usd":num|null,"error":str|null,
 "progress":[{"role":"…","stage":"…","message":"…","ts":num}]}
```

`progress` is the last 12 journalled events from the job's workspace. Unknown
id → `404 {"error":"unknown job"}`.

### `GET /jobs/{id}/result` (auth) — result

- **succeeded**, assess:
  `{"kind":"assess","success":bool,"classification":str|null,`
  `"executive_summary":str,"report_path":str|null,"report_markdown":str,`
  `"cost_usd":num}`
- **succeeded**, deliver: `{"kind":"deliver", …delivery fields…}`.
- **succeeded**, verify:
  `{"kind":"verify","source_job":str,"finding_id":str,`
  `"verdict":"confirmed|refuted|needs-context","rationale":str,`
  `"citations":[{"path":str,"note":str}],"cost_usd":num}`.
- **failed**: `{"kind":<mode>,"success":false,"error":str,"cost_usd":num}` —
  the real (possibly partial) spend banked before the failure; `0` only when
  the job failed before any budget existed (e.g. a clone failure).
- **cancelled**: `{"kind":<mode>,"success":false,"error":"cancelled","cost_usd":0}`.
- still **queued/running**: `409 {"error":"not finished","state":<state>}`.
- unknown id → `404`.

State machine: `queued → running → succeeded | failed`, plus `queued →
cancelled` (see *Cancel* below) — `cancelled` is reachable only from
`queued`, never from `running`.

### `POST /jobs/{id}/backlog` (auth, no body) — generate the backlog later

Turns a finished assess job's persisted findings into backlog stories — a
pure disk transform: **no agents, no LLM calls, $0**, answered synchronously
(no queue slot). It reads `audit/{id}/assessment.json` from the dashboard
workspace and merges the stories into that workspace's
`.dev_team/backlog.json`, deduplicated by title, so repeat calls are
idempotent (`stories_added` drops to `0`).

```json
{"job_id":"assess-…","stories_added":7,"stories_total":7}
```

Errors:

- `404 {"error":"no assessment for that job"}` — no
  `audit/{id}/assessment.json` in the dashboard workspace. Note the
  retroactivity caveat: jobs assessed **before** this feature existed never
  persisted one, so they 404 — re-assess to get the file.
- `409 {"error":"backlog generation needs a dashboard workspace"}` — the
  service was started without `--dashboard-workspace`.
- `401` — as everywhere.

Because it reads only the persisted JSON (never the in-memory registry),
this endpoint keeps working for jobs that ran **before a service restart** —
assess once, generate the backlog any time.

## Cancel

The box has exactly one worker, so a mis-submitted or no-longer-wanted job
sitting in `queued` has no way out except waiting for it to run anyway or
restarting the whole service (which drops the entire in-memory queue,
including every other legitimately queued job). Cancel is the missing rung
on the job lifecycle: a one-way `queued → cancelled` transition, mirroring
the archive/unarchive precedent's shape (in-memory mutation guarded by the
existing lock, not a new store).

### `POST /jobs/{id}/cancel` (auth, no body)

→ `200 {"id":"assess-…","state":"cancelled"}`. Errors:

- `404 {"error":"unknown job"}` — no such job id.
- `409 {"error":"job is not queued","state":<state>}` — the job is
  `running` (an in-flight clone/agent session is out of scope for this
  lighter-weight lifecycle op, same boundary `archive_job` draws) or
  already terminal (`succeeded`/`failed`/`cancelled`). **Not idempotent**:
  cancelling an already-cancelled job is a `409`, matching every other
  one-way state-machine transition in this file — only archive/unarchive
  is idempotent, because it flips a boolean flag rather than progressing a
  state machine.
- `401` — as everywhere.

A cancelled job never reaches `run_job`: no clone, no workspace, no disk
I/O, no agent/LLM call, $0 cost. `GET /jobs/{id}/result` on a cancelled job
returns `200 {"kind":<mode>,"success":false,"error":"cancelled","cost_usd":0}`
rather than the `409 not finished` a genuinely pending job gets. Cancelling
also frees the queue-cap slot the job was holding (`GET /jobs` and the
`POST /jobs` cap only count jobs still `state: "queued"`).

`GET /jobs` lists a cancelled job like any other — it is not auto-archived,
so the record of *why* the queue was shorter stays visible. However, a
cancelled job is cancelled before `run_job` (and therefore the meta.json
mirror) ever runs, so `POST /jobs/{id}/archive` on a cancelled job still
`404`s with `{"error":"no assessment for that job"}` — the same pre-existing
limitation `archive_job` already has for every `deliver` job today, not
something cancel changes.

**Concurrency**: `cancel_job` shares the same lock as the worker's
`queued → running` transition, so the two are mutually exclusive —
whichever call wins a race decides the outcome deterministically. A job can
never end up both running and marked cancelled.

## Interactive deliver

By default a dispatched `deliver` job runs fully autonomously: plan review,
re-plan supervision, and failed-task escalation all take their default
(non-interactive) answer with no human in the loop, exactly as if the CLI's
`--interactive` flag had never been passed. Submitting `interactive: true`
(see `POST /jobs` above) wires the run to a `QueueChannel`-backed
`InteractionChannel` instead, so those three touchpoints pause and wait for
an answer over the two endpoints below — the same primitive
`docs/INTERACTION.md` names as the integration point for a non-terminal UI.

This only unlocks plan review / re-plan / failure-escalation questions. The
feature-commit and guarded-command approval gate stays exactly
`PolicyApprovalGate(block_risks=("high",))` regardless of `interactive` — a
push/deploy/rm still cannot be approved through this (or any) dispatch
surface.

### `GET /jobs/{id}/question` (auth) — peek the pending question

Non-destructive: reads the channel's live question without consuming from
its internal queue, so polling never steals the answer from the run itself.

No pending question — the job was never interactive, isn't paused right
now, or its question was already answered:

```json
{"pending": false}
```

A question is live (`ask()` is blocked on it):

```json
{"pending": true, "prompt": "Approve this plan (3 task(s))?",
 "context": "Plan: …", "default": "approve",
 "choices": [{"key": "approve", "label": "start the work", "accepts_text": false},
             {"key": "revise", "label": "request changes", "accepts_text": true},
             {"key": "abort", "label": "stop the run", "accepts_text": false}]}
```

`404 {"error":"unknown job"}` — no such job id. `401` — as everywhere.

### `POST /jobs/{id}/answer` (auth) — answer the pending question

Body: `{"choice": "approve", "text": ""}` (`text` optional, default `""`,
carries revision/retry guidance into the *next* agent prompt — only
meaningful for a `revise`/`retry`-style choice).

→ `202 {}` on success — the waiting `ask()` call unblocks and the run
proceeds. Errors:

- `404 {"error":"unknown job"}` — no such job id.
- `409 {"error":"no pending question"}` — the job was never interactive, or
  is interactive but has no live pause right now. Never a silent no-op
  `202`.
- `400 {"error":"unknown choice"}` — `choice` is not one of the *live*
  question's choice keys, validated against a closed set and never treated
  as free-form or forwarded anywhere. Nothing is pushed to the run in this
  case, so a mistyped choice leaves the run still waiting, retryable.
- `401` — as everywhere.

**Fail-secure on abandonment**: if nobody answers within
`interactive_timeout_seconds` (clamped/defaulted per `POST /jobs` above),
the question's default choice is taken automatically and the run proceeds
exactly as a non-interactive job would have — a dead dashboard or absent
operator degrades to autonomous, never hangs.

## Archive / unarchive

Test/demo runs and superseded re-assessments accumulate with no lifecycle —
worse, a stale or fabricated verification pollutes the `GET /calibration`
rollup. Archiving hides a job (and its stories/verdicts) from every listing
without deleting anything: the data stays on disk and is fully restorable.
**This is metadata-only** — for permanent deletion see *Purge* below.

### `POST /jobs/{id}/archive` (auth, no body)

Sets `archived: true` (+ `archived_at`) on `audit/{id}/meta.json` — the same
mirror `verify`/`make_backlog` already read, so no new store is introduced.

→ `200 {"id":"assess-…","archived":true}`. Errors:

- `404 {"error":"no assessment for that job"}` — no persisted `meta.json`
  (predates the feature, was never mirrored, or the id is malformed/
  traversal-shaped — a job id is never used to build a path outside its own
  `audit/{id}/` directory).
- `409 {"error":"job is running"}` — the job is `queued` or `running` in the
  in-memory registry; its files are still being written by the single-flight
  worker. Unarchiving has no such restriction.
- `409 {"error":"archive needs a dashboard workspace"}` — no
  `--dashboard-workspace` configured.
- `401` — as everywhere.

### `POST /jobs/{id}/unarchive` (auth, no body)

Clears the marker. **Idempotent**: unarchiving a job that is not archived is
still `200 {"id":"…","archived":false}`, not an error. Same 404/409-workspace
errors as archive (never the running-job 409).

### Effect elsewhere

- `GET /jobs`, `GET /calibration`, and `GET /costs` exclude an archived job
  (see above).
- The dashboard's activity feed, Reports panel, and Kanban board (stories
  carrying that job's `source_job`) exclude it too, with a "show archived"
  toggle to reveal it again — see [`docs/DASHBOARD.md`](DASHBOARD.md).
- Concurrency: archiving is guarded by the same rule that already protects
  `meta.json` — the single-flight worker writes it exactly once, while the
  job is still `queued`/`running`, and archiving a `queued`/`running` job is
  refused above, so the worker and an archive/unarchive call never race.
  What remains is concurrent archive/unarchive calls against the same job,
  which the dispatcher serialises with a dedicated lock.

## Purge (permanent deletion)

Archiving hides; it never reclaims disk. A long-running deployment
accumulates a workspace clone per job forever (see *Deployment* below), and
the only way to reclaim that space was, until now, shell access and a manual
`rm -r`. `POST /jobs/{id}/purge` is the operator-facing, narrow, **archive-
gated** alternative: permanent deletion made an explicit two-step action
(archive, confirm, then purge) rather than a single-click irreversible one.

v1 removes exactly three things, each independently testable:

- the job's workspace clone (`jobs_root/{id}`) — `shutil.rmtree`, since this
  directory already sits outside the `Workspace` abstraction (the same raw
  path join the worker itself uses to materialise it);
- the `audit/{id}/` mirror in the dashboard workspace (`assessment.md`,
  `assessment.json`, `meta.json`, `verifications.jsonl`) — each removed
  through `Workspace.delete()`, **never** a raw filesystem call, so the
  existing traversal/symlink-escape guard on that abstraction still applies;
- backlog stories whose `source_job` is this job, removed under the same
  write lock `DELETE /backlog/story/{id}` already uses, dependency edges
  stripped the same way.

`events.jsonl` and transcripts are **out of scope for v1** — hand-filtering
an append-only, size-bounded journal is a separable, harder-to-test surface.

### `POST /jobs/{id}/purge` (auth, no body)

→ `200 {"id":"assess-…","purged":true,"removed":{"workspace":true,"audit":true,"backlog_stories":2}}`.
Each `removed.*` field reflects what was actually found and deleted — `false`/`0`
if that piece was already gone (a job purged after someone already ran a
manual `rm -r` on its clone, say), never an error. Errors:

- `404 {"error":"unknown job"}` — no such job (unknown id, or already
  purged: purge is **not idempotent**, unlike unarchive — a second call on
  the same id is a 404, never a redundant 200).
- `409 {"error":"job is running"}` — the job is `queued` or `running`;
  checked directly against the in-memory record within one locked block
  (never by re-entering the archive check's own lock — see *Concurrency*
  below).
- `409 {"error":"job is not archived"}` — the job is terminal but was never
  archived. Archive it first.
- `401` — as everywhere.

### Concurrency and deadlock safety

`purge_job` acquires the dispatcher's registry lock exactly once, checking
`record.state` directly rather than calling the same internal helper
`archive_job` uses to make that check (which itself acquires the same lock —
calling it from inside an already-held lock would hang the calling thread
forever, and because every other mutation in this service shares that lock,
freeze the entire single-flight dispatcher). The registry entry is deleted
inside that same locked block, so a second purge call always sees "unknown
job", never a redundant success. The backlog-story removal is a separate,
independent lock — the same one `DELETE /backlog/story/{id}` uses — so a
purge and a concurrent board write can never interleave into a corrupt
`backlog.json`.

### Natural growth (not in v1)

- Fold `events.jsonl`/transcript surgery into the purge once this pattern is
  proven.
- A scheduled/TTL auto-purge policy over already-archived jobs past N days.
- Bulk purge (`?archived_before=`) once the single-job primitive has real
  usage to generalise from.

## Finding re-verification (mode `verify` + two read routes)

An assess job's claims are model output. The `verify` mode has a **fresh
skeptical agent** re-check ONE persisted finding against a fresh clone of
the source job's repository: read-only tools, refute-first instructions, a
closed `confirmed|refuted|needs-context` verdict set with citations. The
full verification model (fresh agent ≠ original author, LLM-phases-only
scope, untrusted-claim handling) is documented in
[`docs/ASSESSMENT.md`](ASSESSMENT.md).

Everything is **disk-keyed** off the dashboard workspace — never the
in-memory registry — so it all survives a service restart:
`audit/<id>/assessment.json` (the findings), `audit/<id>/meta.json` (which
repo to re-clone), `audit/<id>/verifications.jsonl` (the verdict history).
All three routes therefore answer
`409 {"error":"… needs a dashboard workspace"}` when the service was
started without `--dashboard-workspace`, and jobs assessed **before this
feature existed** lack `meta.json` and answer
`404 {"error":"no assessment for that job"}` on submit — re-assess once to
record it.

### `GET /jobs/{id}/findings` (auth) — enumerate re-checkable claims

```json
{"job_id":"assess-…","findings":[
  {"id":"risk.secrets[0]","phase":"risk","role":"security-engineer",
   "claim":"connection string committed","evidence":"Web.config",
   "hash":"1f0c94ab23de","citation_broken":false}]}
```

Positional ids (`phase.list[i]`; component deep-dives nest as
`components.components[i].findings[j]`); only phases that completed are
enumerated, and the deterministic `dead_code`/`dependency_scan` outputs are
excluded (exact program results, not model claims). `hash` is a short
content hash of the claim so a caller can spot drift between enumeration
and verification. `citation_broken` is `true` when the finding's `evidence`
is one the $0 citation check already flagged as a cited path that doesn't
exist in the repo (see `broken_citations` in the assess phase output) — a
signal to help triage which findings are worth spending a real `verify` job
on, never a substitute for one; it degrades to `false` on any missing or
malformed data (including assessments persisted before this field existed).
`404 {"error":"no assessment for that job"}` when
`audit/{id}/assessment.json` is absent.

### `POST /jobs` with `{"mode":"verify",…}` — submit a re-check

`{"mode":"verify","source_job":<assess job id>,"finding_id":<finding id or
case-insensitive claim substring>,"budget_usd":number|null,
"skip_broken_citations":bool|omitted,"votes":int|omitted}` →
`202 {"id":"verify-…","state":"queued","position":n}`. The finding is
resolved synchronously at submit time; unless `skip_broken_citations`
short-circuits the job (below), it re-clones the repo named by the source
job's `meta.json`. Errors: `400` (missing/blank `source_job`/`finding_id`,
`skip_broken_citations` present and not a bool, or `votes` present and not
an integer in `[1, 5]` — a bool, string, float, or out-of-range value is
rejected, never coerced),
`404 {"error":"no assessment for that job"}`,
`404 {"error":"finding not found"}`,
`409 {"error":"verify needs a dashboard workspace"}`.

`skip_broken_citations` (default `false`) mirrors `dev-team --verify
--skip-broken-citations`: when `true` and the resolved finding's
`citation_broken` is `true`, the job completes at **$0** with **no clone
and no agent call** — the repo named by `meta.json` is never touched, so
there is no network egress, disk I/O, or clone latency for it either —
`verdict` is always `needs-context` (a broken citation impugns the
citation, never proves the claim false) and the result additionally
carries `"success":true,"skipped":true` so a caller can tell a $0
deterministic skip apart from a real agent verdict. A skipped result is
**not** appended to `GET /jobs/{source-id}/verifications` or counted by
`GET /calibration` — no model ever adjudicated it, so persisting it would
silently dilute `confirm_rate`.

`votes` (default `1`, unchanged behaviour) mirrors `dev-team --verify
--verify-votes`: runs `votes` independent skeptical agent passes
concurrently and takes the plurality verdict (a tie resolves to
`needs-context`, never promoted to a confirmation or refutation), capped at
**5** on both the CLI and this dispatch surface via one shared constant —
an uncapped value would let a single request fan out an unbounded burst of
concurrent agent calls against the shared pool. The result additionally
carries `"votes":[{"verdict","rationale","citations"}]` (one entry per
completed pass) and `"vote_count":int` when `votes > 1`; the top-level
`verdict`/`rationale`/`citations` fields are unchanged, so
`GET /jobs/{source-id}/verifications` and `GET /calibration` (which only
ever read the top-level `verdict`) need no change and never double-count a
multi-vote result as more than one entry.

The job then flows through the normal machinery: single-flight queue,
`GET /jobs/{id}` status, and `GET /jobs/{id}/result` (shapes above — a
verifier that itself fails is a failed job:
`{"kind":"verify","success":false,…}`; a `refuted` verdict is a
*successful* verification).

### `GET /jobs/{source-id}/verifications` (auth) — verdict history

```json
{"job_id":"assess-…","verifications":[
  {"finding_id":"risk.secrets[0]","verdict":"confirmed","rationale":"…",
   "citations":[{"path":"Web.config","note":"…"}],"cost_usd":0.04,
   "ts":1789345678.0}]}
```

Chronological (append order under the single-flight worker); empty list for
an assessed job never verified; `404 {"error":"no assessment for that
job"}` for an unknown id.

### `GET /calibration` (auth) — verdict calibration rollup, across every job

A pure, $0, disk-only aggregate over **every** persisted verification, not
just one job's: walks `audit/*/verifications.jsonl` in the dashboard
workspace, groups entries by the phase prefix of their `finding_id`
(`"risk.secrets[0]"` → `risk`), and counts `confirmed`/`refuted`/
`needs_context` per phase and overall. A job marked archived (see
*Archive / unarchive* above) is skipped entirely, so a stale or fabricated
verification stops skewing the rollup the moment its job is archived, and
resumes contributing the moment it is unarchived.

```json
{"phases":{"risk":{"confirmed":6,"refuted":1,"needs_context":1,
                     "total":8,"confirm_rate":0.75}},
 "overall":{"confirmed":6,"refuted":1,"needs_context":1,
            "total":8,"confirm_rate":0.75},
 "jobs_counted":3}
```

`confirm_rate` is `confirmed / total` (`null` when `total` is 0). An entry
whose verdict falls outside the closed `confirmed|refuted|needs-context` set,
or whose `finding_id` is missing/non-string, is dropped rather than trusted —
the same fail-secure posture the `verify` write path applies, re-applied
here at read time. A corrupt (non-JSON) line is skipped, not a 500 — same
tolerant parse as `GET /jobs/{id}/verifications`. `jobs_counted` is the
number of `verifications.jsonl` files that contributed at least one
parseable line. `409 {"error":"calibration needs a dashboard workspace"}`
when the service was started without `--dashboard-workspace`.

### `GET /costs` (auth) — total spend rollup, across every job

A pure, $0, in-memory aggregate over every job's `cost_usd` — the registry,
not disk, is the source of truth here: unlike verdicts, `deliver` job cost is
never mirrored to disk, so a disk walk (as `/calibration` does) would
silently under-report. Only `succeeded` and `failed` jobs have a non-`null`
`cost_usd` (`queued`/`running` never set it; a cancelled job never touches
it) — those are the only ones counted:

```json
{"total_usd":12.34,"by_mode":{"assess":8.0,"deliver":3.34,"verify":1.0},
 "jobs_counted":15}
```

`by_mode` includes only modes with at least one counted job (an empty
registry gives `{"total_usd":0.0,"by_mode":{},"jobs_counted":0}`). Archived
jobs (see *Archive / unarchive* above) are excluded by default; `?archived=1`
includes them, matching `GET /jobs`'s toggle. Unlike `/calibration`, `/costs`
needs **no** dashboard-workspace guard — it works standalone off the
registry, and archived-exclusion simply no-ops (never excludes anything)
until `--dashboard-workspace` is configured.

## Access log

Every HTTP request the service receives — `GET /health`, an authorised
route, a `401` auth miss, or a `404` on an unrecognised path — appends
exactly one record to a bounded JSONL journal at `<jobs_root>/access.jsonl`
(`/opt/dev-team/jobs/access.jsonl` by default), created lazily on the first
request. This closes CLAUDE.md section 7's log-gap requirement ("every agent
authentication and call must produce a retained, reviewable log") for the
one HTTP surface in this repo that otherwise silences its per-request
logging entirely (`Handler.log_message` is a deliberate no-op — the CLI
prints the bind URL once instead of a line per request).

```json
{"ts":1789345678.0,"method":"GET","path":"/jobs","status":200}
```

Fields are deliberately minimal:

- `ts` — the record's wall-clock time (`time.time()`, or the dispatcher's
  injected `clock` in tests).
- `method` — the HTTP method (`GET`, `POST`, `PATCH`, `DELETE`).
- `path` — the request's URL path only (no query string), truncated to at
  most 2048 bytes so a maximal-length request line cannot inflate a single
  entry disproportionately — independent of the HTTP server's own implicit
  request-line cap.
- `status` — the HTTP status code actually sent for that request.

**Deliberately never logged**: the `Authorization` header value (or any
other header), and any request or response body. A `deliver` job's
`title`/`description` is caller-supplied free text that could carry
anything the caller pastes — logging *that* a call happened, on what path,
with what outcome, is the goal; logging its payload is explicitly out of
scope for this journal (a future authenticated read route could add
correlation with a job id, but the log itself never stores bodies).

Bounded like `.dev_team/events.jsonl` (`dev_team.eventlog.EventLog`): once
the file exceeds 4000 lines it is rewritten keeping the newest half, so a
long-running service never accretes an unbounded file, including under
sustained hostile traffic (even just repeated auth misses). Appends are
lock-serialised (`dev_team.accesslog.AccessLog`), so concurrent requests —
this is a `ThreadingHTTPServer` — never lose an entry to a lost-update race.
A log-write failure (disk full, unwritable `jobs_root`) is swallowed at the
handler level and never turns an otherwise-successful response into a
crash; it also never affects the response already sent to the caller,
since the record is appended only after `send_response` has been called.

### `GET /access-log` (auth) — recent access records

A newest-first page of the journal above, so the tailnet-only deployment
(see *Deployment* below) can be glanced at from the dashboard instead of
requiring an SSH session to `cat access.jsonl`:

```json
{"entries":[{"ts":1789345680.0,"method":"GET","path":"/jobs","status":200},
            {"ts":1789345678.0,"method":"GET","path":"/whatever","status":404}]}
```

`?limit=` defaults to 100 and is clamped to `[1, 1000]`; a missing or
non-numeric value falls back to the default rather than erroring — the same
clamp-not-reject posture `interactive_timeout_seconds` uses. A missing or
empty `access.jsonl` (a fresh deployment with zero requests logged yet)
answers `200 {"entries":[]}`, never an error, and a corrupted/non-JSON line
mixed into the journal is skipped rather than failing the whole request —
both inherited directly from :func:`read_access_log`'s existing tolerant-read
contract. No dashboard-workspace guard needed: `access.jsonl` lives under
`jobs_root`, independent of `--dashboard-workspace`, exactly like `GET
/costs`. Every entry carries exactly the four fields the write path
persists (`ts`/`method`/`path`/`status`) — never an `Authorization` header
or a request/response body, since those are never written in the first
place (see above).

## Deployment

`deploy/dev-team-dispatch.service` is a hardened, singleton systemd unit
(`User=devteam`, `ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges`,
`ProtectHome`, `ReadWritePaths=/opt/dev-team`, …). It is ordered after and
requires `tailscaled.service` and binds the **tailnet IP only** — the service
authenticates but also holds Claude credentials and runs agent code, so it is
never exposed to the public internet. `DEV_TEAM_DISPATCH_TOKEN` (and the
Claude credentials) live in the unit's `EnvironmentFile`
(`/etc/dev-team/dev-team.env`). Each job's clone lives under
`/opt/dev-team/jobs/<id>`.

To capture each agent call's raw I/O for the dashboard, set
`DEV_TEAM_RECORD_TRANSCRIPTS=1` in that `EnvironmentFile` (or pass
`--record-transcripts`). It is **off by default** and records sensitive raw
repo content — see [`docs/TRANSCRIPTS.md`](TRANSCRIPTS.md).
