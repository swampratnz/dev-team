# Troubleshooting

A symptom-first runbook for operating the standing `dev-team` services
(deployment, dispatch, dashboard) and the self-improvement pipeline. This
page collects operational knowledge that already lives in
[`DEPLOYMENT.md`](../DEPLOYMENT.md), [`docs/DISPATCH.md`](DISPATCH.md),
[`docs/DASHBOARD.md`](DASHBOARD.md), and [`docs/PIPELINE.md`](PIPELINE.md)
and indexes it by the symptom an operator actually searches for. Those docs
remain the authoritative source for each subsystem — this page cross-links
them rather than duplicating them.

## "401 Invalid bearer token" right after a service starts

This is almost always the env-file gotcha, not a bad token. Unlike a shell
`source`, systemd's `EnvironmentFile` does **not** strip a trailing `#`
comment — a credential line followed by an inline `# my token` comment
makes the comment part of the token value, and the very first Claude call
then fails with a confusing `401 Invalid bearer token`.

**Fix:** open the unit's env file (e.g. `/etc/dev-team/health.env` or
`/etc/dev-team/dev-team.env`) and put every comment on its own line — no
value may have a trailing inline `#` comment. See the full callout in
[`DEPLOYMENT.md`](../DEPLOYMENT.md) (sections 5b and 5c) for the exact
service files this affects.

## "My submitted job vanished" / "the queue looks wrong after a restart"

The dispatch service has exactly one worker and keeps its queue **in
memory**. Restarting the service (`systemctl restart
dev-team-dispatch.service`, a crash, a host reboot) drops:

- every job still sitting in `queued` (not yet started) — gone, with no
  trace;
- the in-memory job registry's view of a job that was `running` at the
  moment of restart.

It does **not** drop anything already persisted to disk under
`/opt/dev-team/jobs/<id>/`:

- `audit/<id>/meta.json` — the job's submitted parameters;
- any assessment report or structured result a **completed** job already
  wrote;
- verification records.

**Before resubmitting**, check whether the job's directory already has a
completed result on disk — if it does, resubmitting duplicates work instead
of recovering it. If the job is still `queued`, cancel just that one with
`POST /jobs/{id}/cancel` (auth) instead of restarting the whole service —
see [`docs/DISPATCH.md`](DISPATCH.md)'s *Cancel* section. A full service
restart is only necessary for a job that has already moved to `running`,
which Cancel does not cover.

## "I need to see the access/request log"

Every HTTP request the dispatch service receives — successful, a `401` auth
miss, or a `404` — is appended to a bounded JSONL log at
`<jobs_root>/access.jsonl` (default `/opt/dev-team/jobs/access.jsonl`,
relative to `--jobs-root`). The primary, documented way to read it remotely
is `GET /access-log` (auth) — see [`docs/DISPATCH.md`](DISPATCH.md)'s
*Access log* section — which returns a newest-first page of the journal
from the dashboard without requiring an SSH session onto the deployment
host. An operator who already has host access, or wants to watch the raw
file grow, can use direct filesystem access as an alternative:

```bash
# Tail the dispatch service's access log as it grows:
tail -f /opt/dev-team/jobs/access.jsonl

# Pull out just the 401s, most recent first:
tac /opt/dev-team/jobs/access.jsonl | jq 'select(.status == 401)'

# The per-workspace event journal (deliver/assess runs), same pattern:
tail -f /path/to/workspace/.dev_team/events.jsonl
```

Both logs are bounded (oldest half dropped past 4,000 lines), so `tail -f`
is safe to leave running — it will not fall behind an unbounded file.

## An issue/PR is labelled `needs-human` — what now?

`needs-human` is the shared escalation lane across six pipeline loops. Where
to look, and which loop applied it, differs by context:

| Loop | Where it escalates | What to check |
|---|---|---|
| research / adversarial | Issue comment | The adversarial verdict comment explaining the rubric failure or ambiguous call |
| build | Issue comment | The final blocker comment explaining which gate could not be made green |
| autofix | PR comment | The CI run logs for the failure it could not resolve in 2 attempts |
| conflict-resolver | PR comment | The merge conflict it could not resolve in one attempt |
| revise | PR comment | The PR review's "Changes requested" verdict it could not satisfy in 2 attempts |
| pr-review | PR comment/label | A "Needs a human decision" verdict on the review itself |

**Safe way to clear the label:** resolve the underlying cause first (merge
the conflict, fix the failing gate, make the human call the loop
escalated), then remove `needs-human` so the pipeline can resume from that
state. Never remove the label as a way to "unstick" a loop without
addressing why it escalated — that only routes the same problem back into
automation to fail again, and defeats the human-oversight gate the label
exists to enforce (see [`docs/PIPELINE.md`](PIPELINE.md)'s ownership rules).

## Dashboard/dispatch HTTP status quick-reference

| Status | Meaning | Source |
|---|---|---|
| `401` | Missing or wrong bearer token / dashboard session | [`docs/DASHBOARD.md`](DASHBOARD.md) (Authentication), [`docs/DISPATCH.md`](DISPATCH.md) (every route) |
| `409` | A state-transition conflict — e.g. archiving a still-`queued`/`running` job, or a backlog request with no dashboard workspace configured | [`docs/DASHBOARD.md`](DASHBOARD.md) (Archived jobs), [`docs/DISPATCH.md`](DISPATCH.md) (backlog) |
| `501` | The dashboard proxy feature isn't configured — no dispatch URL/token wired up, so board editing, job actions, or the cost rollup answer "not configured" instead of erroring | [`docs/DASHBOARD.md`](DASHBOARD.md) (The board write model, costs panel) |
| `502` | The dashboard's proxy to the dispatch service couldn't reach it (dispatch service down/unreachable) | [`docs/DASHBOARD.md`](DASHBOARD.md) (The board write model) |

Each row links to the section that documents that code in full — this table
is an index, not a replacement for reading the source section.
