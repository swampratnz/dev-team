# The dispatch service (`--dispatch`)

An authenticated HTTP API that lets an external caller drive the team
remotely: **submit** an assess or deliver job against a repository, **poll**
its status, and **fetch** the result. It wraps the same code paths as
`dev-team --assess` / `--deliver` — clone the repo, build a `DevTeam`, run it.

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
Reports panel. The job's own workspace stays the source of truth; this is a
read-only visibility copy. Point the dashboard and the dispatcher at the same
`DIR` (e.g. `/opt/dev-team/workspace`) to watch dispatched runs live.

## API

Base `http://<host>:<port>`. All bodies are JSON.

### `GET /health` (no auth)

```json
{"status":"ok","service":"dev-team-dispatch","version":"0.7.0"}
```

### `POST /jobs` (auth) — submit

Body:

```json
{"mode":"assess|deliver","repo":"owner/name or url",
 "title":"...","description":"...","budget_usd":10}
```

- `mode` must be `assess` or `deliver`.
- `repo` is a GitHub `owner/name` slug or any git URL (must parse).
- `budget_usd` is `null` or a positive number.
- `deliver` requires a non-empty `title` and `description`.
- `assess` defaults `title` to the repo slug and `description` to `""`.

→ `202 {"id":"assess-…","state":"queued","position":0}`. Errors:
`400 {"error":…}` (bad mode/repo/budget, missing title or description for
deliver, malformed JSON), `401`, `503 {"error":"queue full"}`.

### `GET /jobs` (auth) — list

Newest-first, capped at 25:

```json
{"jobs":[{"id":"…","mode":"assess","repo":"…","state":"…",
          "started":123.0,"ended":null}]}
```

### `GET /jobs/{id}` (auth) — status

```json
{"id":"…","mode":"assess","repo":"…",
 "state":"queued|running|succeeded|failed",
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
- **failed**: `{"kind":<mode>,"success":false,"error":str,"cost_usd":0}`.
- still **queued/running**: `409 {"error":"not finished","state":<state>}`.
- unknown id → `404`.

State machine: `queued → running → succeeded | failed`.

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
