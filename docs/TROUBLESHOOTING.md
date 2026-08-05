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

- the registry entry for every job still sitting in `queued` (not yet
  started);
- the in-memory job registry's view of a job that was `running` at the
  moment of restart.

It does **not** drop anything already persisted to disk under
`/opt/dev-team/jobs/<id>/`:

- `audit/<id>/meta.json` — the job's submitted parameters, written the
  moment the job is **submitted**, not only on completion;
- any assessment report or structured result a **completed** job already
  wrote;
- verification records.

So the job is not gone without trace: `GET /jobs/{id}` after a restart still
resolves it from `meta.json` — `state: "interrupted"` if it had not
succeeded yet, `state: "succeeded"` with its full result if it had (assess
mode only) — see [`docs/DISPATCH.md`](DISPATCH.md)'s *Restart survival*
notes on `GET /jobs/{id}` and `/result`.

**Before resubmitting**, check whether the job's directory already has a
completed result on disk (or query `GET /jobs/{id}`, which now surfaces the
`"interrupted"` state directly) — if it does, resubmitting duplicates work
instead of recovering it. If the job is still `queued`, cancel just that one
with `POST /jobs/{id}/cancel` (auth) instead of restarting the whole service
— see [`docs/DISPATCH.md`](DISPATCH.md)'s *Cancel* section. A full service
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

## "A backlog story is stuck `blocked` and never resumes"

This is expected, not a bug. The backlog foreman gives each story exactly
**one** autonomous attempt — see [`docs/DISPATCH.md`](DISPATCH.md)'s *The
backlog foreman* section. A story lands in `blocked` when its
foreman-enqueued job failed, timed out, or completed without a successful
delivery, and `blocked` stories are **never re-selected** — they wait for a
human by design.

**To see why:** the story carries `delivery_job`, the id of the job the
foreman enqueued for it. Fetch that job's outcome with
`GET /jobs/{id}/result` (auth) — see [`docs/DISPATCH.md`](DISPATCH.md)'s job
result route.

**To resume it:** move the story back to `todo` with
`POST /backlog/story/{id}/status` (auth) — see
[`docs/DASHBOARD.md`](DASHBOARD.md)'s *The board write model* section, which
documents this route in full. Once it's `todo` again, the next
`POST /foreman/run` batch is free to re-select
it as if it were new.

## "My --interactive-pr-comments reply never got answered / the CI-fix round skipped anyway"

This is almost always one of two by-design behaviours of
`GitHubPRCommentChannel`, not a hang or a crash — see the "Supervising CI
fixes from the pull request (`--interactive-pr-comments`)" section of
[`docs/INTERACTION.md`](INTERACTION.md) (docs/INTERACTION.md) for the full
mechanism.

**A reply is silently ignored** if either check fails:

- the commenter's GitHub login isn't in the `--interactive-pr-comment-author`
  allow-list passed at startup (case-insensitive, no implicit default — an
  empty list matches nobody); or
- the reply's first whitespace-trimmed, lower-cased word isn't exactly
  `` `apply` `` or `` `skip` `` — there is no fuzzy matching, so `"applying
  now"` or `"Apply this"` (extra words) does not count as a match.

Neither case logs anything the operator sees or posts an acknowledgement —
the channel just keeps polling as if no reply had arrived.

**The round fails safe to `skip` once the poll window is exhausted** (30
polls at a 20-second interval by default — about 10 minutes). This is not a
missed fix: the CI fix is deliberately *not* force-applied without a
blessed reply, mirroring the console channel's own end-of-input fail-safe.

**How to confirm this is what happened, not a hang:** nothing else needs
checking — this channel only ever replaces the single `ci_fix_question`
asked after a PR is open; plan review, escalation, and approval questions
on the console terminal are a separate channel and are unaffected. Reply
again from an allow-listed login with exactly `apply` or `skip` as the
first word of a fresh comment before the next poll window closes.

## `docker_build_verified` / `docker_run_verified` is false — is my delivery broken?

No. Both `--docker-build-gate` and `--docker-run-gate`
(`EngineConfig.docker_build_gate` / `docker_run_gate`) are **advisory only**
— see [`docs/BENCHMARKS.md`](BENCHMARKS.md)'s DevOps section for the design
rationale: it never blocks, fails, or rolls back a delivery, even when the
underlying build or run itself fails. A run can finish green while its
scorecard shows `docker_build_verified: false` or `docker_run_verified:
false`, and that is not a contradiction.

**The three states you'll see on each scorecard key:**

- **Absent** — the gate is off (both are off by default; pass
  `--docker-build-gate` / `--docker-run-gate` at the CLI to turn one on).
- **`True`** — the build (`docker_build_verified`) or the smoke-tested
  container (`docker_run_verified`) succeeded.
- **`False`** — something did not succeed; check the matching `_detail`
  scorecard key for what happened:
  - `docker_build_detail` — the `docker build` invocation failed (event
    `docker-build-failed`); truncated build output is captured there.
  - `docker_run_detail` — one of two distinct causes, both surfaced under
    `docker_run_verified: False`:
    - the `docker run` invocation itself never started (missing/
      misconfigured local docker daemon, a stale container-name collision,
      ...) — recorded as event `docker-run-start-failed`, with no
      grace-period wait, since there is nothing yet to wait on; or
    - the container started but exited before the fixed grace period
      (`_DOCKER_RUN_GATE_GRACE_SECONDS`, 3.0s) elapsed — recorded as event
      `docker-run-failed` (not `docker-run-verified`, which only ever fires
      when the container is still running), with `docker_run_detail`
      captured from `docker logs`.

**`docker_build_verified: False` under `--sandbox` is expected, not a
bug.** The gate reuses the same contained `command_runner` as every other
gate, which typically has no docker socket/network — so a build simply
cannot succeed there. See [`docs/BENCHMARKS.md`](BENCHMARKS.md) (DevOps)
for the full design note.

**Do not "fix" a `False` reading by relaxing containment.** Because
`--docker-run-gate` starts an image built from an untrusted, adversarial
repo's own `ENTRYPOINT`, its `docker run` invocation always launches with
`--network none --cap-drop ALL --security-opt no-new-privileges` — these
flags are unconditional and are not the cause of a `docker_run_verified:
False` result; look at `docker_run_detail` instead.

## Dashboard/dispatch HTTP status quick-reference

| Status | Meaning | Source |
|---|---|---|
| `401` | Missing or wrong bearer token / dashboard session | [`docs/DASHBOARD.md`](DASHBOARD.md) (Authentication), [`docs/DISPATCH.md`](DISPATCH.md) (every route) |
| `409` | A state-transition conflict — e.g. archiving a still-`queued`/`running` job, or a backlog request with no dashboard workspace configured | [`docs/DASHBOARD.md`](DASHBOARD.md) (Archived jobs), [`docs/DISPATCH.md`](DISPATCH.md) (backlog) |
| `500` | POST /foreman/run's post-submit backlog write failed — the just-enqueued jobs were compensated (cancelled) rather than left to double-spend on a re-run | [`docs/DISPATCH.md`](DISPATCH.md) (The backlog foreman) |
| `501` | The dashboard proxy feature isn't configured — no dispatch URL/token wired up, so board editing, job actions, or the cost rollup answer "not configured" instead of erroring | [`docs/DASHBOARD.md`](DASHBOARD.md) (The board write model, costs panel) |
| `502` | The dashboard's proxy to the dispatch service couldn't reach it (dispatch service down/unreachable) | [`docs/DASHBOARD.md`](DASHBOARD.md) (The board write model) |

Each row links to the section that documents that code in full — this table
is an index, not a replacement for reading the source section.
