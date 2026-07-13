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
| **Reports** — every `audit/*.md`, viewable in place | the workspace tree |

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
  forwards **only** those two actions, never a general `/jobs` passthrough.
  Archiving a job that is still `queued`/`running` is rejected (`409`) by
  the dispatch service itself.

### Story detail (click a backlog story)

Every backlog story is clickable (mouse or Enter/Space). The modal shows
the story's full description, estimate, status, the epic it belongs to,
and — when the story was generated from an assessment finding — where it
came from:

- A story bred from an **LLM finding** carries its source job and finding
  id (`recommendation.plan[0]`, `risk.secrets[1]`, …) and shows a copyable
  **`dev_team_verify <source_job> <finding_id>`** one-liner: it re-checks
  that single claim with a fresh, skeptical agent against a clean clone,
  fully independent of the auditor that wrote it.
- A **deterministic** story (dependency scan, dead-code probe) is exact
  program output, not a model claim — the modal says so and suggests
  re-running the assessment to refresh it.

Story titles and descriptions are repository-derived content; like reports
and transcripts, the modal renders them escape-first, so markup or scripts
inside a finding display as inert text.

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

Archive/unarchive (above) is a second, equally narrow proxy of the same
shape:

```
browser ──(dashboard token)──▶ dashboard /api/jobs/{id}/archive|unarchive ──(dispatch token)──▶ dispatch /jobs/{id}/archive|unarchive
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
  gates the archive/unarchive proxy: `/api/jobs/{id}/archive` and
  `/api/jobs/{id}/unarchive` are the **only** two actions forwarded (never a
  general `/api/jobs/*` passthrough), and without a token they answer
  `501 {"error": "job actions not configured"}`.
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
- `POST /api/jobs/{id}/archive` / `POST /api/jobs/{id}/unarchive` — the
  archive/unarchive proxy described above (same auth and `501` gating).
