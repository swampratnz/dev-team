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
| **Backlog** — one epic **per assessed repository** ("Remediation — \<repo\>") with story-point progress bars and story status chips (todo / ▶ in progress / ✓ done / ✕ blocked); clickable stories (see below) | `.dev_team/backlog.json` |
| **Memory** — run count, recent retrospectives, ADR titles | `.dev_team/memory.json` |
| **House conventions** — the captured style summary | `.dev_team/conventions.json` |
| **Reports** — every `audit/*.md`, viewable in place | the workspace tree |

Stat tiles across the top summarise runs recorded, open/done/blocked
stories, and time since the last activity.

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

## API

Everything the page shows is plain JSON, usable by your own tooling (add
the bearer header when a token is set):

- `GET /api/state` — the full dashboard state document.
- `GET /api/report?path=audit/assessment.md` — a report's markdown (only
  paths the workspace actually lists are served).
- `POST /login` (form field `token`) / `POST /logout` — the browser cookie
  session lifecycle described above.
