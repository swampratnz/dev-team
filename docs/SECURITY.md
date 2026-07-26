# Security reference

dev-team's day job is running agents against **untrusted third-party
repositories** and exposing a **bearer-authed remote API**, so its security
model is load-bearing, not incidental. That model is implemented across six-plus
files (`docs/SANDBOX.md`, `docs/DISPATCH.md`, `docs/DASHBOARD.md`,
`docs/ASSESSMENT.md`, `docs/ROADMAP.md`, `CHANGELOG.md`), each covering one
mechanism in isolation. This doc is the map: one section per threat area,
naming the exact current module/function that implements it and pointing at
the detailed doc for depth. It never duplicates that doc's prose — read it for
"how", read this for "where" and "does it exist".

A reviewer (human or the pipeline's own PR-review worker, see
[`docs/PIPELINE.md`](PIPELINE.md)) should be able to check a new diff against
every section below before approving.

## Untrusted-content & prompt-injection handling

Untrusted text shown to an agent — file bodies, diffs, tool/scanner output,
cross-run memory digests, audit finding claims — is wrapped in delimited
`<...>` blocks the system prompt declares off-limits. A hostile string
containing that block's own closing tag could otherwise end the block early
and have whatever follows read as trusted instructions.

`dev_team.fences.defuse` neutralises exactly that: it inserts a
zero-width space into each named closing tag, invisible to a human but no
longer a structural match, and is idempotent. It backs every untrusted-content
site in the codebase — `context.py`, `retrieval.py`, `engine.py`,
`assessment.py`, and the `reviewer`/`security`/`architect`/`manager`/
`retrospector` agents (see the CHANGELOG's "Prompt-fence defusing is now
systemic" entry for the full site list). A new site that renders repo-derived
or model-origin text inside a delimited block must call `defuse` on it before
handing it to an agent — that is the standing invariant a reviewer checks.

## Credential & token hygiene

Git credentials never touch argv, `.git/config`, or a subprocess's persisted
environment. `dev_team.sources.git_auth_env` builds a per-command,
GitHub-HTTPS-only basic-auth header carried via `GIT_CONFIG_*` env variables
scoped to one subprocess call; `dev_team.sources.scrub_credentials`
redacts both the raw token and its derived base64 header value from any
command output before it can leak into a log or an error. The two are always
paired — every git call that carries the header (clone, and the delivery
target's push) scrubs with it before raising.

Env-derived secrets (the GitHub PAT, the dispatch/dashboard bearer tokens) are
read from an env file via `dev_team.sources.load_env_file`, never
committed, never placed in prompt or agent context (CLAUDE.md §2). The
dispatch service's `DEV_TEAM_DISPATCH_TOKEN` and the dashboard's
`DEV_TEAM_DASHBOARD_TOKEN` are both compared with `hmac.compare_digest`
(constant-time) — see *HTTP surface auth* below.

## Execution containment

Running agent-authored tests, or a project's own build/test commands, is
arbitrary code execution. `dev_team.sandbox.ContainerCommandRunner`
wraps another `CommandRunner` and boxes every non-`git` command inside a
`docker`/`podman run` invocation via `dev_team.sandbox.SandboxConfig`:
no network by default, all capabilities dropped, no-new-privileges, resource
ceilings (memory/cpu/pids), only the workspace bind-mounted, and only the
caller-supplied env forwarded through a mode-`0600` env file (never inline
`--env`, never argv). git porcelain self-delegates to the host unchanged —
see [`docs/SANDBOX.md`](SANDBOX.md) for the full trust-boundary table and the
`--sandbox` CLI wiring.

This is ROADMAP item 1's containment layer, and its own known open edge is
named in *What this does NOT protect against* below.

## Workspace & path containment

`dev_team.execution.LocalWorkspace.delete` (and every other path-taking
`LocalWorkspace` method) routes through `_within_root`, which resolves the
target's real path and confirms it still falls under the workspace root's real
path — catching a symlink that points outside the root even after the
textual `..`/absolute-path rejection in `_normalise` already ran. `list_files`
applies the same check to skip any symlink whose real path escapes the root.
The dispatch service's `purge_job` reuses this containment boundary when
resolving a job's on-disk directory before deleting it.

## Agent tool-loop containment

The checks above cover the orchestrator's own file I/O; they say nothing
about the agentic engineer's *own* SDK tool loop (Bash/Read/Write/Edit/Glob/
Grep via the Claude CLI), which runs on the host outside `CommandRunner` and
was, until now, scoped only by `cwd` — a working-directory default, not an
access boundary. `dev_team.agent_sandbox.workspace_containment_hook` closes
that gap with a `PreToolUse` hook (`ClaudeAgentOptions.hooks`) that denies any
Bash/Read/Write/Edit/Glob/Grep call whose target resolves outside the session's
workspace root, reusing the same real-path/symlink-escape check as
`LocalWorkspace._within_root` above. `dev_team.sdk` wires it automatically
whenever a call carries a `cwd` — the same condition that turns an SDK call
into a real tool loop in the first place (see `AgentRunner`) — so both the
persistent-session path (`DeliveryEngine._open_engineer_session`, rooted at
the task's own worktree in worktree mode) and the cold `implement_in_place`
path (the shared `ClaudeAgentRunner`'s per-call `cwd` override) are covered
without either call site having to remember to opt in. A denial never
includes any filesystem path in its reason (root or sibling), so it cannot
leak another job's layout back to the model.

Read/Write/Edit/Glob are checked deterministically against the tool call's
own structured path argument — no false positives or negatives. **Bash is
different: it is a best-effort string scan for path-like tokens (absolute
paths, `..` segments, `cd` targets), not a shell parser, and is evadable by a
sufficiently adversarial one-liner** (environment-variable expansion,
base64, `eval`, and similar tricks are not defeated by it). It is
defense-in-depth, not a hard boundary — treat it accordingly.

## HTTP surface auth

Both HTTP surfaces require a bearer token compared with `hmac.compare_digest`
(constant-time, never logged or reflected):

- **Dispatch** — every route except `GET /health` requires
  `Authorization: Bearer <token>` against `DEV_TEAM_DISPATCH_TOKEN`; a
  missing/empty token at startup is a hard error, so the service never runs
  unauthenticated. Implemented in `dev_team.dispatch._make_handler`. See
  [`docs/DISPATCH.md`](DISPATCH.md).
- **Dashboard** — with `DEV_TEAM_DASHBOARD_TOKEN` set, every route requires it
  (bearer header for API callers, a `HttpOnly; SameSite=Strict` session cookie
  for browsers). Implemented in `dev_team.dashboard._tokens_match` /
  `dev_team.dashboard._make_handler`. Left unset, the dashboard stays
  open — see the gap this leaves, below. See [`docs/DASHBOARD.md`](DASHBOARD.md).
- **Dashboard → dispatch proxy** is deliberately narrow: only
  `/api/backlog/*` (board writes), `/api/jobs/{id}/archive|unarchive|purge`,
  and `GET /api/costs` are forwarded, each to the single matching dispatch
  route, with the dispatch token attached server-side and never handed to the
  browser. There is no general `/api/jobs/*` or arbitrary-route passthrough.
- **Repeated bad-token requests are throttled.** Both surfaces share the same
  discipline: a source IP that racks up `--auth-rate-limit-threshold` wrong
  tokens (default 10) within `--auth-rate-limit-window-seconds` (default 60)
  is locked out for `--auth-rate-limit-lockout-seconds` (default 60) —
  further requests from that source get `429 {"error": "too many failed
  attempts", "retry_after": N}` with a `Retry-After` header, and the token
  comparison itself never runs while locked out. Dispatch gates this ahead of
  `_authenticate` (both the operator-token comparison and the OAuth
  session-token lookup branch count as failures); the dashboard gates it in
  both `_authorised` (bearer/cookie) and `POST /login` (the form), sharing one
  tracker per process so probing either path draws on the same budget.
  Implemented in `dev_team.authguard.FailedAuthTracker` — in-memory only,
  keyed on source IP (never the attempted token), bounded to
  `max_tracked_sources` (default 4096) distinct sources. `--auth-rate-limit-
  threshold 0` disables the guard (byte-identical to the prior unthrottled
  behaviour); a negative value for any of the three flags is a startup
  error. Keying on source IP assumes the tailnet-only deployment model this
  document and `docs/DISPATCH.md` already document — behind a reverse proxy
  or shared NAT, every caller collapses to one source IP and shares one
  lockout budget, a shared-fate risk that is a consequence of the deployment
  model, not of the throttle itself.

## Pipeline/CI guardrails

The self-extension pipeline ([`docs/PIPELINE.md`](PIPELINE.md)) that builds
this repo carries its own guardrails so an agent loop can never reach
production alone:

- Every workflow checks out with `persist-credentials: false` and uses a
  per-step `GH_TOKEN` env instead, so no git credential is persisted to disk
  for a later step (or a later, compromised process) to find.
- No worker's `--allowedTools` grants a blanket `git:*`, `gh:*`, or
  `python:*` — each allowed command is enumerated explicitly (e.g.
  `Bash(git push origin HEAD)`, `Bash(gh pr create:*)`), and none of them is
  `gh pr merge` or `gh api`.
- Fork PRs are excluded from secrets: workflows that push or comment run only
  against same-repo, non-fork branches (checked explicitly, e.g.
  `isCrossRepository == false`), so a hostile fork PR can never trigger a
  privileged action with this repo's tokens.
- Branch protection on `main` (blocking direct and force pushes, requiring
  PRs) is the structural backstop — **no loop merges a PR**; a human always
  does.

## What this does NOT protect against

- **Per-job isolation on a shared host.** `ContainerCommandRunner` boxes what
  a *single* dispatched job's commands can reach, but ROADMAP item 1 names the
  remaining gap explicitly: one dispatched job's container can still see
  another's workspace on a shared host, because there is no per-job
  rootless-container or namespace boundary yet. The engineer's own SDK tool
  loop now has a filesystem-level trust boundary of its own (see *Agent
  tool-loop containment* above) — deny-by-default outside its workspace root,
  with the Bash side of that check an evadable heuristic, not a hard
  guarantee — but that is still an in-process check, not an OS-level
  isolation boundary: the process-level container/VM recipe in
  `DEPLOYMENT.md` §5d remains the real isolation boundary for a shared host
  until the per-job container/namespace work lands.
- **The dashboard's unauthenticated-by-default localhost stance.** With
  `DEV_TEAM_DASHBOARD_TOKEN` unset, every dashboard route — including the
  event journal, backlog, memory, and any markdown report, plus recorded
  agent transcripts if enabled — stays open with no login and no bearer
  check. That is an intentional default for local, loopback-only development,
  not a hardened one — but it is the *only* case that stays that way: binding
  beyond `127.0.0.1` (`--host 0.0.0.0` or a routable address) without setting
  a token now fails closed (`DevTeamError`, the server never starts) unless
  the operator explicitly passes `--allow-unauthenticated-dashboard` to opt
  back into the old warn-and-serve behavior. Treat an unauthenticated
  dashboard as safe only on localhost.

This doc reflects the mechanisms as of this writing; if it and the code it
cites ever disagree, trust the code and file a correction here.
