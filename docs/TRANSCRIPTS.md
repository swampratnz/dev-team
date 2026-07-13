# Agent I/O transcripts

Transcripts capture the **raw input and output of every agent call** so you can
see exactly what each agent was told and what it said — the full system prompt,
the prompt, the response, and the call's cost. They are **off by default** and
surface per-agent in the dashboard's agent modal.

> **Security — read this first.** A transcript contains the raw content of the
> assessed/delivered repository (including any secrets committed to that repo)
> and the model's verbatim reply. **Whenever recording is enabled — or the
> dashboard binds beyond localhost — set `DEV_TEAM_DASHBOARD_TOKEN`** so every
> dashboard route (the transcript routes included) requires the token via
> bearer header or the browser login cookie (see
> [DASHBOARD.md](DASHBOARD.md#authentication-opt-in-token)). This token is a
> stopgap until an Auth0/IdP layer lands. Keep the bind on a trusted network
> regardless, and treat the `.dev_team/transcripts/` directory as sensitive.

## What it captures

For each agent call, one JSON file with:

- `ts`, `run`, `role`, `seq` — when, which run, which agent, and the per-role
  call sequence (so the dashboard correlates a transcript with the agent's
  event timeline: the run id matches the event journal's).
- `system_prompt`, `prompt`, `response` — the raw I/O (each capped, see below).
- `cost_usd`, `is_error` — the call's cost and whether it returned an error
  result.

A call that *raises* (never returns a result) is **not** recorded — there is
nothing to record. Recording is best-effort: a write failure is swallowed and
never breaks a run, exactly like the event journal.

## Enabling it

- **CLI (`--assess` / `--deliver`):** add `--record-transcripts`.

  ```bash
  dev-team --assess --repo acme/mono --record-transcripts
  dev-team "Feature" "Description" --deliver --workspace ./build --record-transcripts
  ```

- **Dispatch service:** pass `--record-transcripts`, **or** set the environment
  variable `DEV_TEAM_RECORD_TRANSCRIPTS` to a truthy value (`1`, `true`, `yes`;
  case-insensitive). The box sets the env var in the unit's `EnvironmentFile`
  so the shipped `deploy/dev-team-dispatch.service` stays OFF/safe by default —
  the operator opts in, nothing in the unit changes.

## Where files land

```
<workspace>/.dev_team/transcripts/<run>/<role>-<NNN>.json
```

- For `--assess` / `--deliver`, `<workspace>` is the run's workspace and
  `<run>` is the same id used for the event journal.
- For **dispatched** jobs, transcripts land where the dashboard can read them:
  the shared `--dashboard-workspace` when one is configured (the same place a
  dispatched job's events and report are mirrored, under the job id as `<run>`),
  otherwise the job's own isolated workspace.

## Size cap

Each of `system_prompt` / `prompt` / `response` is truncated to `max_chars`
(default **200,000** characters) with a trailing ` …[truncated N chars]`
marker, so a runaway prompt or response can never fill the disk.

## Viewing them

Open the dashboard (`dev-team --dashboard --workspace DIR`), click an agent to
open its history modal, and expand the **Transcripts (N)** subsection. Each
recorded call is a collapsible showing its seq, cost and time; expanding it
loads the full **System prompt / Prompt / Response** in scrollable monospace
blocks. When nothing is recorded you'll see a muted hint on how to enable it.

Transcript text is raw repository-derived content, so the dashboard
**HTML-escapes every field** before it reaches the DOM and renders it as inert,
verbatim text (a `<script>` or `</pre>` in a prompt cannot break out). The read
routes (`/api/transcripts`, `/api/transcript`) sanitise every query parameter
and gate on workspace membership as a traversal guard, mirroring `/api/report`.
