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
| **Backlog** — epics with story-point progress bars, story status chips (todo / ▶ in progress / ✓ done / ✕ blocked) | `.dev_team/backlog.json` |
| **Memory** — run count, recent retrospectives, ADR titles | `.dev_team/memory.json` |
| **House conventions** — the captured style summary | `.dev_team/conventions.json` |
| **Reports** — every `audit/*.md`, viewable in place | the workspace tree |

Stat tiles across the top summarise runs recorded, open/done/blocked
stories, and time since the last activity.

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
dashboard is **read-only but unauthenticated** — it exposes the event
journal, backlog, memory, and any markdown report in the workspace to
whoever can reach it. Keep the default localhost bind unless the network is
trusted, or put it behind a reverse proxy that adds auth.

## API

Everything the page shows is plain JSON, usable by your own tooling:

- `GET /api/state` — the full dashboard state document.
- `GET /api/report?path=audit/assessment.md` — a report's markdown (only
  paths the workspace actually lists are served).
