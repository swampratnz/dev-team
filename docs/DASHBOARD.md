# The dashboard (`--dashboard`)

A local web page over a workspace: what every agent is doing right now, what
it last worked on, the backlog, cross-run memory, and the assessment reports
— refreshed live while runs happen.

```bash
dev-team --dashboard --workspace /path/to/repo
# dev-team dashboard for /path/to/repo at http://127.0.0.1:8737/ (Ctrl-C to stop)
```

The dashboard is a **separate process**. Start it once and leave it open;
every `--deliver` and `--assess` run against the same workspace journals its
progress events to `.dev_team/events.jsonl`, and the page picks them up on
its next refresh (every 2.5s). Nothing needs to be running for the page to
be useful — backlog, memory, conventions, and reports are read from the
workspace on every request.

## What it shows

| Panel | Backed by |
|-------|-----------|
| **The team** — one card per agent: persona, current stage, last message, how long ago | `.dev_team/events.jsonl` (journaled by every run) |
| **Activity** — the newest events across all agents and engines | same journal |
| **Runs** — recent runs with their last message and event counts | same journal |
| **Backlog** — an interactive **Kanban board** per epic (one epic **per assessed repository**, "Remediation — \<repo\>"): four columns (To do / In progress / Blocked / Done) with per-column counts, a muted Declined row, story-point progress bars, dependency indicators, and clickable cards (see below) | `.dev_team/backlog.json` (writes via the dispatch proxy) |
| **Memory** — run count, recent retrospectives, ADR titles | `.dev_team/memory.json` |
| **House conventions** — the captured style summary | `.dev_team/conventions.json` |
| **Verdict calibration** — per-phase and overall confirmed/refuted/needs-context counts and confirm rate | `audit/<id>/verifications.jsonl` (same aggregate as `GET /calibration`) |
| **Spend** — total spend and a per-mode breakdown, fetched on demand | the dispatch service's `GET /costs` (proxied, see *Spend* below) |
| **Access log** — recent dispatch HTTP requests (method/path/status), fetched on demand | the dispatch service's `GET /access-log` (proxied, see *Access log* below) |
| **Reports** — every `audit/*.md`, viewable in place, with blind-spot/broken-citation count chips (see *Report quality chips* below) | the workspace tree |

Runs, Reports, and the Kanban board all exclude **archived** jobs by
default (see *Archived jobs* below) — a "show archived" toggle above the
Runs panel reveals them.

Stat tiles across the top summarise runs recorded, open/done/blocked/declined
stories, and time since the last activity.

### The Kanban board

Each epic renders as a horizontal board — **To do / In progress / Blocked /
Done** columns (counts in the header) plus a de-emphasised **Declined** row.
A card shows its title, points, and a **dependency indicator**: `⛓ N` when
the story has `depends_on` edges, and a distinct **"blocked by unfinished
\<title\>"** flag while any dependency is not yet done or declined.

With the write proxy configured (see below), the board is editable:

- **Move** — the `<select>` on every card (and in the card modal) posts the
  new status to `/api/backlog/story/{id}/status`.
- **Decline / Delete** — buttons in the card modal (`.../decline`;
  `DELETE .../story/{id}` behind a confirm step).
- **Edit** — the modal's form PATCHes title / description / estimate.
- **Add card** — the "＋ Add card" button under each epic opens a small
  form (title required) and POSTs `/api/backlog/story` with that `epic_id`.
- **Dependencies** — the modal's checkbox list (other cards in the same
  epic; never itself) posts `.../deps`; the server rejects unknown ids and
  cycles, and the rejection message is shown inline.

Every card field — titles, descriptions, dependency titles, even the
dispatch service's error messages — is repo-derived or round-trips through
the server, so the page escapes all of it before it reaches the DOM
(`esc()` / `textContent`, never raw `innerHTML`). Without a dispatch token
the controls answer `501` and the board is effectively read-only.

### Archived jobs

Test/demo runs and superseded re-assessments accumulate with no lifecycle,
and a stale or fabricated verification pollutes the dispatch service's
`GET /calibration` rollup. **Archive** (see [`docs/DISPATCH.md`](DISPATCH.md))
hides a job's activity, report, and backlog stories without deleting
anything — the data stays on disk and is fully restorable with
**unarchive**.

- A "show archived" checkbox above the **Runs** panel toggles
  `GET /api/state?archived=1`, which reveals archived runs, reports, and
  backlog stories (each marked with a dashed "archived" chip) alongside the
  live ones.
- Each **run row** and **report row** carries an archive/unarchive button.
  Clicking it POSTs `/api/jobs/{id}/archive` or `/api/jobs/{id}/unarchive`
  through the dashboard — the same narrow-proxy pattern the board write path
  uses (below): the dispatch bearer token stays server-side, and the proxy
  forwards **only** those actions, never a general `/jobs` passthrough.
  Archiving a job that is still `queued`/`running` is rejected (`409`) by
  the dispatch service itself.

### Purge (permanent deletion)

Archiving hides; it never reclaims disk (see
[`docs/DISPATCH.md`](DISPATCH.md) for the full model). Each **run row**
that is already archived also carries a **"delete permanently"** button,
behind the same two-step confirm the backlog's story-delete button uses
(first click arms it, second click purges). Clicking it POSTs
`/api/jobs/{id}/purge` through the same narrow jobs proxy described above.

The button only renders for a job the dashboard already knows is archived —
that is a UX nicety, not the security boundary: the dispatch service's own
`purge_job` re-enforces the archive-first gate server-side (`409` on a
non-archived job) regardless of what the browser sends. Purge is **not
idempotent** — a second call on an already-purged job answers `404`, the
job having left every listing for good.

### Story detail (click a backlog story)

Every backlog story is clickable (mouse or Enter/Space). The modal shows
the story's full description, estimate, status, the epic it belongs to,
and — when the story was generated from an assessment finding — where it
came from:

- A story bred from an **LLM finding** carries its source job and finding
  id (`recommendation.plan[0]`, `risk.secrets[1]`, …) and shows a copyable
  one-liner that submits the dispatch service's `mode:"verify"` job — a
  `curl -sX POST /jobs` with the `{"mode":"verify","source_job":…,
  "finding_id":…}` body documented in [`docs/DISPATCH.md`](DISPATCH.md)
  (the repo to re-clone comes from the source job's `meta.json`, so no repo
  is sent). It re-checks that single claim with a fresh, skeptical agent
  against a clean clone, fully independent of the auditor that wrote it.
- A **deterministic** story (dependency scan, dead-code probe) is exact
  program output, not a model claim — the modal says so and suggests
  re-running the assessment to refresh it.

Story titles and descriptions are repository-derived content; like reports
and transcripts, the modal renders them escape-first, so markup or scripts
inside a finding display as inert text.

### Calibration

The **Verdict calibration** panel (next to House conventions) shows the same
rollup the dispatch service's `GET /calibration` computes (see
[`docs/DISPATCH.md`](DISPATCH.md)) — a table of per-phase
confirmed/refuted/needs-context counts and confirm rate, plus an overall
row — computed in-process from `audit/<id>/verifications.jsonl` on the
shared workspace tree rather than proxied to a running dispatch service, so
it works even when the dashboard is running standalone. It respects the
same archived-job exclusion and "show archived" toggle as the rest of the
page, and renders a muted empty state until the first verification is
recorded.

Above the verdict table, a one-line summary folds in the same blind-spot
and broken-citation totals `GET /calibration` now returns (see
[`docs/DISPATCH.md`](DISPATCH.md)): e.g. *"5 blind spots · 2 broken
citations across 2 audits"*. This line renders whenever either total is
non-zero — including when there are zero verifications recorded yet, so a
fresh assessment's report-quality signals are never hidden behind the
verdict table's own empty state — and renders nothing when both totals are
zero, keeping a clean run's panel uncluttered.

### Report quality chips

Each row in the **Reports** panel additionally shows up to two chips, read
straight from the mirrored `audit/<id>/assessment.json` next to the report
itself (no dispatch call, same in-process pattern as calibration above):
a muted **"N blind spots"** chip when the audit's `blind_spots` list is
non-empty (top-level directories no phase finding ever cited), and a
critical **"N broken citations"** chip when `broken_citations` totals
above zero across phases (a citation naming a file path not actually
present in the repo). The two chips are independent — a report can show
either, both, or neither depending on its own counts — and a clean audit's
row renders with no chips at all, same as today.

Opening a report also prepends a small **"Audit quality"** block above the
rendered markdown, listing each blind-spot directory and each broken
citation verbatim. `broken_citations` values are a finding's own claimed
evidence string a *model* wrote, not a deterministic path like
`blind_spots`, so both are escaped through `esc()` before touching
`innerHTML` — the same precedent the Access log panel's `path` field
already established for caller/model-influenced text.

### Spend

The **Spend** panel (next to Memory & conventions) shows the dispatch
service's total spend and a per-mode breakdown — the same rollup
`GET /costs` computes (see [`docs/DISPATCH.md`](DISPATCH.md)). Unlike
calibration, this is **proxied to the dispatch service, not computed
in-process**: `deliver` job cost is never mirrored to disk (only `assess`
jobs get a mirrored `meta.json`), so an in-process disk walk would silently
under-report total spend by omitting every `deliver` job. The dashboard
therefore forwards `GET /api/costs` to the running dispatch service's
`GET /costs`, the same proxy shape the archive/unarchive/purge actions
already use (see *The board write model* below).

The panel is fetched **once on page load plus a manual refresh button** —
deliberately **not** part of the 2.5s `/api/state` poll every open
dashboard tab runs, since folding a proxied network hop into that poll
would multiply dispatch-service load by (open tabs) every 2.5s for a
number that only changes when a job finishes. Without a dispatch URL/token
configured, `GET /api/costs` answers `501` and the panel renders a muted
"not configured" state, never a raw error.

### Access log

The **Access log** panel (next to Spend) shows the dispatch service's most
recent HTTP requests — method, path, and status, status colour-coded so a
run of `401`s is visually obvious — the same page `GET /access-log` returns
(see [`docs/DISPATCH.md`](DISPATCH.md)). The dispatch service is deployed
tailnet-only behind a hardened systemd unit, so this panel is what lets an
operator notice a misconfigured caller or a credential-stuffing burst from
the dashboard they already have open, instead of SSHing in to `cat
access.jsonl`.

Same proxy shape and the same on-demand-only discipline as Spend: fetched
**once on page load plus a manual refresh button**, never part of the 2.5s
poll (a proxied dispatch hop must not multiply by open tabs × poll cadence
for data that only changes on a new request). Without a dispatch URL/token
configured, `GET /api/access-log` answers `501` and the panel renders a
muted "not configured" state. Every rendered field goes through `esc()`
before `innerHTML` — a logged `path` is arbitrary caller-supplied input (an
external caller can hit `/whatever<script>` and have it logged verbatim, by
design), so it must always render as inert text.

### Foreman plan

The **Foreman plan** panel (next to Access log) shows the backlog foreman's
$0 dry-run — the same dependency-ready-stories preview `GET /foreman/plan`
computes (see [`docs/DISPATCH.md`](DISPATCH.md)): each `todo` story whose
dependencies are all `done`/`declined`, the repo it resolves to, and (for an
ineligible story) why not. It answers "what would `/foreman/run` enqueue
right now" without spending anything or enqueueing anything — `POST
/foreman/run` itself is **not** wired to the dashboard; that write stays a
future, separate slice (its own budget/max-stories form and confirm step).

Same proxy shape and the same on-demand-only discipline as Spend/Access log:
`GET /api/foreman/plan` forwards to the dispatch service's `GET
/foreman/plan` (`?max_stories=` passed through unchanged, letting the
dispatch service's own `[1, 10]` clamp handle an out-of-range or non-numeric
value), fetched **once on page load plus a manual refresh button**, never
part of the 2.5s `/api/state` poll (a proxied dispatch hop must not
multiply by open tabs × poll cadence). Without a dispatch URL/token
configured, `GET /api/foreman/plan` answers `501` and the panel renders a
muted "not configured" state; a `plan: []` response (nothing ready to
deliver) renders a muted empty state rather than an empty table. An
ineligible story (`eligible: false`) shows its `reason` in place of a repo.
Every rendered field (`story_id`, `title`, `repo`, `reason`) goes through
`esc()` before `innerHTML` — a story's title can trace to an LLM assessment
finding per the existing Story-detail provenance model, so it is treated as
untrusted exactly like every other panel's caller- or model-derived text.

### Pending questions

An `interactive: true` deliver job (see [`docs/DISPATCH.md`](DISPATCH.md))
pauses mid-run waiting for an operator's answer. Each run card for a
currently-**running**, non-archived job carries a **pending question**
panel: when the job is paused, it shows the live prompt/context and one
button per choice (a choice with `accepts_text` gets a small text input
alongside its button); clicking a button submits that choice (and any
typed text) and clears the panel.

This is the dashboard-side "questions as buttons" surface `docs/ROADMAP.md`
item 7 names, built on the interactive primitive
[#87](https://github.com/swampratnz/dev-team/issues/87) added to the
dispatch service. Two new proxies back it, the same narrow shape as the
Spend proxy above:

```
browser ──(dashboard token)──▶ dashboard GET /api/jobs/{id}/question ──(dispatch token)──▶ dispatch GET /jobs/{id}/question
browser ──(dashboard token)──▶ dashboard POST /api/jobs/{id}/answer ──(dispatch token)──▶ dispatch POST /jobs/{id}/answer
```

- `GET /api/jobs/{id}/question` is read-only, forwarded verbatim to
  `<dispatch-url>/jobs/{id}/question` (including an unknown job id's
  `404`); without a token it answers `501 {"error": "pending question not
  configured"}`.
- `POST /api/jobs/{id}/answer` forwards its JSON body (`{"choice": ...,
  "text": ...}`) byte-for-byte to `<dispatch-url>/jobs/{id}/answer` and
  relays the response verbatim (`400`/`409`/`202`); without a token it
  answers `501 {"error": "job actions not configured"}` — the same message
  the archive/unarchive/purge proxy uses, since this is still a job action,
  just a body-forwarding one kept out of that no-body action set (see
  *The board write model* below). Untrusted `choice`/`text` are never
  validated dashboard-side — the dispatch service already validates
  `choice` against the *live* question's closed key set, so this proxy adds
  no new trust boundary.
- **Scope is exact**: only `.../question` and `.../answer` match — a path
  that merely starts with the jobs prefix (e.g. `.../question/extra`,
  `.../answered`) falls through to the ordinary `404`, never into either
  proxy.

**Polling is scoped and visibility-gated**, not part of the 2.5s
`/api/state` poll: the dashboard polls `GET .../question` only for jobs
currently shown as running (excluding archived ones) and only while
`document.visibilityState` is `"visible"`, on its own 5s interval —
mirroring the Spend panel's reasoning above, since most running jobs are
never actually interactive and this keeps that common case at zero extra
dispatch calls. Without a dispatch URL/token configured, `GET
.../question` answers `501` and the panel renders nothing, the same
degrade-gracefully contract as Spend/Calibration.

## The event journal

Runs journal automatically — every `--deliver` or `--assess` invocation
appends timestamped events (role, stage, message, detail, persona, run id)
to `.dev_team/events.jsonl` in the workspace. The journal is bounded (the
oldest half is dropped past 4,000 lines) and reads are forgiving: a corrupt
line is skipped, never fatal. Library users can journal too — pass an
`EventLog(workspace, run="...")` as (or composed into) the engine's
`listener`.

## Serving beyond localhost

`--port` picks the port (default 8737); `--host 0.0.0.0` binds wider. The
dashboard is read-only but exposes the event journal, backlog, memory, any
markdown report — and, when recording is enabled, the raw
[agent transcripts](TRANSCRIPTS.md) — to whoever can reach it. **Set a
token whenever the bind is non-local or transcripts are enabled.** Binding
beyond loopback without one prints a stderr warning (it does not refuse, so
existing localhost-adjacent setups keep working).

## Authentication (opt-in token)

Set `DEV_TEAM_DASHBOARD_TOKEN` before starting the dashboard and **every
route requires it**:

```bash
DEV_TEAM_DASHBOARD_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  dev-team --dashboard --workspace /path/to/repo --host 100.x.y.z
```

- **Browsers** get a login page on any unauthenticated page request; a
  correct token sets a `devteam_dash` session cookie
  (`HttpOnly; SameSite=Strict; Path=/`) and redirects to the dashboard.
  `POST /logout` clears it.
- **API callers** send `Authorization: Bearer <token>`; unauthenticated
  `/api/*` requests get `401 {"error": "unauthorized"}`.
- Comparison is constant-time (`hmac.compare_digest`), the token is never
  logged or reflected in a response, and it never appears in a URL. Pick a
  URL/cookie-safe value (e.g. `secrets.token_urlsafe`) — the cookie value is
  the token verbatim.
- **Rotation:** change the env var and restart; existing cookies stop
  working immediately.
- Empty/unset keeps the dashboard **open**, exactly as before — for
  localhost development only.

This is a stopgap until an IdP (Auth0) integration lands; the seam it
replaces is `Handler._authorised` (plus the `/login` flow) in
`dashboard.py`.

## The board write model (backlog editing)

The dashboard stays a **read-only viewer of the workspace** — it never
writes `backlog.json` itself. Board edits flow through a deliberately
narrow proxy to the **dispatch service**, which owns every backlog write:

```
browser ──(dashboard token)──▶ dashboard /api/backlog/* ──(dispatch token)──▶ dispatch /backlog/*
```

Archive/unarchive/purge (above) is a second, equally narrow proxy of the
same shape:

```
browser ──(dashboard token)──▶ dashboard /api/jobs/{id}/archive|unarchive|purge ──(dispatch token)──▶ dispatch /jobs/{id}/archive|unarchive|purge
```

The Spend panel's `GET /api/costs` (above) is a third, read-only proxy of
the same shape:

```
browser ──(dashboard token)──▶ dashboard GET /api/costs ──(dispatch token)──▶ dispatch GET /costs
```

The Access log panel's `GET /api/access-log` (above) is a fourth, read-only
proxy of the same shape:

```
browser ──(dashboard token)──▶ dashboard GET /api/access-log ──(dispatch token)──▶ dispatch GET /access-log
```

The pending-question panel's two routes (above) are a fifth pair, one
read-only and one body-forwarding:

```
browser ──(dashboard token)──▶ dashboard GET /api/jobs/{id}/question ──(dispatch token)──▶ dispatch GET /jobs/{id}/question
browser ──(dashboard token)──▶ dashboard POST /api/jobs/{id}/answer ──(dispatch token)──▶ dispatch POST /jobs/{id}/answer
```

The Foreman plan panel's `GET /api/foreman/plan` (above) is a sixth,
read-only proxy of the same shape — and, deliberately, `POST /foreman/run`
is **not** wired to any dashboard route:

```
browser ──(dashboard token)──▶ dashboard GET /api/foreman/plan ──(dispatch token)──▶ dispatch GET /foreman/plan
```

- **The proxy** (`--dashboard` with `--dispatch-url`, default
  `http://127.0.0.1:8738`): authorised `POST`/`PATCH`/`DELETE` requests
  under `/api/backlog/` are forwarded — same method, same JSON body — to
  `<dispatch-url>/backlog/...` with `Authorization: Bearer
  $DEV_TEAM_DISPATCH_TOKEN`. The dispatch response (status + JSON body,
  including 400/404/409 rejections) is relayed verbatim; an unreachable
  dispatch service answers `502`. Scope is **strictly `/api/backlog/*`** —
  no other dispatch route (job submission, results) is reachable through
  the dashboard, and the dispatch token is never logged, echoed, or handed
  to the browser. With `DEV_TEAM_DISPATCH_TOKEN` unset the board is
  read-only and writes answer `501 {"error": "board editing not
  configured"}`.
- The same `--dispatch-url`/`DEV_TEAM_DISPATCH_TOKEN` configuration also
  gates the archive/unarchive/purge proxy: `/api/jobs/{id}/archive`,
  `/api/jobs/{id}/unarchive`, and `/api/jobs/{id}/purge` are the **only**
  actions forwarded (never a general `/api/jobs/*` passthrough), and
  without a token they answer `501 {"error": "job actions not
  configured"}`.
- Same again for `GET /api/costs`: forwarded verbatim to `<dispatch-url>
  /costs` (`?archived=1` passed through unchanged, any other/absent value
  excludes archived jobs, matching `GET /jobs`), and without a token it
  answers `501 {"error": "spend rollup not configured"}`. Scope is
  **exactly `/api/costs`** — no path parameter, no other dispatch route
  reachable through it.
- Same again for `GET /api/access-log`: forwarded to `<dispatch-url>
  /access-log`, with `?limit=` passed through unchanged (the dispatch
  service itself clamps/defaults it), and without a token it answers
  `501 {"error": "access log not configured"}`. Scope is **exactly
  `/api/access-log`** — no path parameter, no other dispatch route
  reachable through it.
- Last, `GET /api/jobs/{id}/question` and `POST /api/jobs/{id}/answer`:
  forwarded verbatim to `<dispatch-url>/jobs/{id}/question|answer`
  (including the dispatch service's own `404`/`400`/`409` cases); without a
  token both answer `501` (`{"error": "pending question not configured"}`
  for the GET, `{"error": "job actions not configured"}` for the POST,
  matching the archive/unarchive/purge proxy's message since it is still a
  job action). Scope is **exactly `.../question` and `.../answer`** — a
  path that merely starts with the jobs prefix but doesn't match either
  suffix exactly is the ordinary `404`, never forwarded.
- Last, `GET /api/foreman/plan`: forwarded to `<dispatch-url>/foreman/plan`
  (including the dispatch service's own `409` when its dashboard workspace
  isn't configured), with `?max_stories=` passed through unchanged (the
  dispatch service itself clamps it to `[1, 10]`), and without a token it
  answers `501 {"error": "foreman plan not configured"}`. Scope is
  **exactly `/api/foreman/plan`** — no path parameter, and in particular
  never `/api/foreman/run`, which stays unreachable through the dashboard.
- **Auth is layered**: the browser authenticates to the dashboard
  (dashboard token / cookie, checked first); the dashboard process — not
  the browser — holds the dispatch bearer token. Both comparisons are
  constant-time.

### The dispatch mutation API (`/backlog`, bearer-authenticated)

| Route | Effect |
|-------|--------|
| `GET /backlog` | the full serialised backlog (the board) |
| `POST /backlog/story` | add a story card (`title` required; optional `description`, `estimate` ≥ 1, `epic_id`, `status`) → `201` + the story |
| `POST /backlog/story/{id}/status` | set `status` (todo / in_progress / done / blocked / declined) |
| `POST /backlog/story/{id}/decline` | shorthand for status → `declined` |
| `POST /backlog/story/{id}/deps` | set `depends_on` edges; unknown/self edges and cycles are `400` |
| `PATCH /backlog/story/{id}` | edit `title` / `description` / `estimate` (only provided keys) |
| `DELETE /backlog/story/{id}` | remove the story and strip its id from every other story's `depends_on` |

Every mutation stamps the story's `updated_at`, answers with the affected
story, and runs **synchronously under a single write lock** shared with the
assess worker's backlog merge — the dispatch service is the sole writer of
the dashboard workspace's `backlog.json`, so concurrent edits and job-driven
merges can never lose each other's updates. Story/epic ids are minted past
the highest suffix ever used, so a deleted id is never reissued to an
unrelated new story. Stories bred from an assessment's remediation plan
arrive pre-chained: each plan story `depends_on` the previous plan story.

The dashboard's Kanban board (above) is the interactive rendering of this
API: every board control calls the proxied `/api/backlog/*` routes.

## API

Everything the page shows is plain JSON, usable by your own tooling (add
the bearer header when a token is set):

- `GET /api/state` — the full dashboard state document. `?archived=1`
  includes archived jobs' activity, reports, and backlog stories (`state`
  always carries `archived_jobs` — every archived id — and
  `include_archived`, reflecting which view this response is).
- `GET /api/report?path=audit/assessment.md` — a report's markdown (only
  paths the workspace actually lists are served).
- `POST /login` (form field `token`) / `POST /logout` — the browser cookie
  session lifecycle described above.
- `POST|PATCH|DELETE /api/backlog/...` — the board write proxy described
  above (requires dashboard auth; `501` until a dispatch token is wired).
- `POST /api/jobs/{id}/archive` / `POST /api/jobs/{id}/unarchive` /
  `POST /api/jobs/{id}/purge` — the archive/unarchive/purge proxy described
  above (same auth and `501` gating).
- `GET /api/costs` — the spend rollup proxy described above (`?archived=1`
  passed through unchanged; same auth and `501` gating; requires dashboard
  auth).
- `GET /api/access-log` — the access-log proxy described above (`?limit=`
  passed through unchanged; same auth and `501` gating; requires dashboard
  auth).
- `GET /api/jobs/{id}/question` / `POST /api/jobs/{id}/answer` — the
  pending-question proxy described above (same auth and `501` gating;
  requires dashboard auth).
- `GET /api/foreman/plan` — the foreman-plan proxy described above
  (`?max_stories=` passed through unchanged; same auth and `501` gating;
  requires dashboard auth). `POST /api/foreman/run` does not exist.
