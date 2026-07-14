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
 "title":"...","description":"...","budget_usd":10,"backlog":false}
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
- `deliver` requires a non-empty `title` and `description`.
- `assess` defaults `title` to the repo slug and `description` to `""`.
- `verify` requires a non-empty `source_job` and `finding_id`; it is
  **validated synchronously against disk** at submit time (missing
  assessment/finding → immediate `404`, no queue slot wasted).

→ `202 {"id":"assess-…","state":"queued","position":0}`. Errors:
`400 {"error":…}` (bad mode/repo/budget, non-boolean backlog, missing title
or description for deliver, missing source_job/finding_id for verify,
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
- **failed**: `{"kind":<mode>,"success":false,"error":str,"cost_usd":0}`.
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

## Archive / unarchive

Test/demo runs and superseded re-assessments accumulate with no lifecycle —
worse, a stale or fabricated verification pollutes the `GET /calibration`
rollup. Archiving hides a job (and its stories/verdicts) from every listing
without deleting anything: the data stays on disk and is fully restorable.
**This is metadata-only** — permanent deletion (`DELETE /jobs/{id}`, backlog
epic/story removal, transcript surgery) is explicitly out of scope; the
pre-existing manual `rm -r` path is unchanged for that.

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
   "hash":"1f0c94ab23de"}]}
```

Positional ids (`phase.list[i]`; component deep-dives nest as
`components.components[i].findings[j]`); only phases that completed are
enumerated, and the deterministic `dead_code`/`dependency_scan` outputs are
excluded (exact program results, not model claims). `hash` is a short
content hash of the claim so a caller can spot drift between enumeration
and verification. `404 {"error":"no assessment for that job"}` when
`audit/{id}/assessment.json` is absent.

### `POST /jobs` with `{"mode":"verify",…}` — submit a re-check

`{"mode":"verify","source_job":<assess job id>,"finding_id":<finding id or
case-insensitive claim substring>,"budget_usd":number|null}` →
`202 {"id":"verify-…","state":"queued","position":n}`. The finding is
resolved synchronously at submit time and the job re-clones the repo named
by the source job's `meta.json`. Errors: `400` (missing/blank
`source_job`/`finding_id`), `404 {"error":"no assessment for that job"}`,
`404 {"error":"finding not found"}`,
`409 {"error":"verify needs a dashboard workspace"}`.

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
