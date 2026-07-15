# Installing dev-team on an Ubuntu server

`dev-team` targets modern Ubuntu LTS (22.04 / 24.04). It is a Python
application built on the Claude Agent SDK, which in turn shells out to the
Claude Code CLI, so a deployment host needs Python, git (delivery runs commit
their work), Node.js (for the CLI), and Claude credentials — either a **Claude
subscription token** from `claude setup-token` or a **Claude API key**.

## 1. Prerequisites

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip python-is-python3 git nodejs npm
# The Agent SDK drives the Claude Code CLI:
sudo npm install -g @anthropic-ai/claude-code
```

`python-is-python3` provides a bare `python` command. A minimal Ubuntu server
ships only `python3`, but delivery **gate/verify commands** (auto-detected from
the workspace, e.g. `python -m pytest`) and **agent-authored tests** routinely
invoke plain `python` — without it those gates fail with "command not found",
so tasks are wrongly reported as failed. (If you prefer not to install it
system-wide, ensure `python` otherwise resolves for the `devteam` user.)

`git` is required at runtime for `--deliver`: the delivery engine manages the
workspace's branches, commits, and per-task worktrees by shelling out to it.

## 2. Authentication

The agents run through the Claude Code CLI, which accepts either credential
below via the environment. `dev-team` checks at startup that one is present
(or that a stored `claude` login exists) and fails fast with guidance
otherwise. Never commit credentials.

### Option A — Claude subscription (`claude setup-token`)

If you have a Claude **Pro, Max, Team, or Enterprise** subscription, you can
run dev-team against it instead of pay-as-you-go API billing. Generate a
long-lived OAuth token (valid for one year):

```bash
claude setup-token
```

The command walks you through an OAuth authorization. On a headless server
over SSH it prints a URL — open it in the browser on your laptop, sign in to
your Claude account, and paste the resulting code back into the terminal. The
token (it is **printed once, not saved anywhere**) then goes in the
environment:

```bash
export CLAUDE_CODE_OAUTH_TOKEN=<token from claude setup-token>
```

Alternatively, run `claude setup-token` on your desktop and copy the token to
the server. When the token expires after a year, generate a new one the same
way.

Notes:

- Usage is billed against your subscription's limits, not per-token API
  pricing, so `--budget-usd` cost accounting is indicative rather than a real
  spend meter.
- Anthropic permits subscription authentication for *your own* use of agents
  you run yourself. Offering claude.ai login to third parties from a product
  built on the Agent SDK requires prior approval — if this server serves
  other users, use an API key. See the
  [authentication docs](https://code.claude.com/docs/en/authentication).

### Option B — Claude API key (pay-as-you-go)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

API-key runs meter real spend, so `--budget-usd` reflects actual cost.

(For completeness the CLI also honours `ANTHROPIC_AUTH_TOKEN` for LLM
gateways, and `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` for cloud
providers; dev-team's startup check accepts any of these.)

## 3. Install the application

```bash
sudo useradd --system --create-home --home-dir /opt/dev-team devteam
sudo -u devteam bash -c '
  cd /opt/dev-team
  python3 -m venv .venv
  . .venv/bin/activate
  pip install --upgrade pip
  pip install /path/to/dev-team    # or: pip install -e /path/to/checkout
'
```

Verify:

```bash
sudo -u devteam /opt/dev-team/.venv/bin/dev-team --help
```

## 4. Run the test suite on the host (optional but recommended)

```bash
sudo -u devteam bash -c '
  cd /path/to/checkout
  . /opt/dev-team/.venv/bin/activate
  pip install -e ".[test]"
  pytest
'
```

The suite is hermetic (no network) and must report **100% coverage**.

## 5a. Run as a container

A `Dockerfile` is included. Pass whichever credential you use straight
through from the host environment:

```bash
docker build -t dev-team:latest .
# Subscription token:
docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN \
    dev-team:latest "Health endpoint" "Add a /health endpoint" --json
# ...or API key: docker run --rm -e ANTHROPIC_API_KEY ...
```

For real delivery, mount a workspace volume and pass `--deliver`. The
container is also the recommended isolation boundary: delivery runs execute
agent-authored tests, so give the container no extra credentials and restrict
its network where possible.

```bash
docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN -v "$PWD/build:/build" \
    dev-team:latest "Health endpoint" "Add a /health endpoint" \
    --deliver --workspace /build --budget-usd 5.0 --json
```

## 5b. Run as a systemd unit

`dev-team` is a task runner rather than a daemon, so it is deployed as a
**templated oneshot unit** you start on demand or from a timer. The unit file
is provided at [`deploy/dev-team@.service`](deploy/dev-team@.service) and reads
its arguments from an environment file.

```bash
sudo cp deploy/dev-team@.service /etc/systemd/system/
sudo mkdir -p /etc/dev-team

# One environment file per job, named to match the instance:
sudo tee /etc/dev-team/health.env >/dev/null <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=<token from claude setup-token>
# ...or instead: ANTHROPIC_API_KEY=sk-ant-...
# For --repo against private GitHub repositories (fine-grained PAT,
# read-only Contents). dev-team also reads it from a file passed as
# --env-file, which keeps it out of the process environment entirely:
# GITHUB_TOKEN=github_pat_...
DEV_TEAM_TITLE=Health endpoint
DEV_TEAM_DESCRIPTION=Add a /health endpoint that returns 200 OK
DEV_TEAM_ARGS=--json
EOF
sudo chmod 600 /etc/dev-team/health.env

sudo systemctl daemon-reload
sudo systemctl start dev-team@health.service
journalctl -u dev-team@health.service -f
```

> **Env-file gotcha:** keep every value on its own line with **no trailing `#`
> comment**. Unlike a shell `source`, systemd's `EnvironmentFile` does **not**
> strip an inline comment — `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-...  # my token`
> makes the comment part of the token, and the run fails with a confusing
> `401 Invalid bearer token` at the first Claude call. Put comments on their
> own lines. See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) if you
> hit this after a service is already running.

To run it on a schedule, pair the service with a systemd timer
(`dev-team@health.timer`).

Note: `--interactive` and `--chat` are terminal features — use them in an
SSH session, not in the unit. A oneshot service has no stdin, and an
interactive prompt with no stdin **fails closed** (plan review aborts, every
approval is denied) rather than running autonomously — so a detached `-i` run
is more likely to stop than to quietly do the work. To drive runs from a web
UI or chat bot instead, see [`docs/INTERACTION.md`](docs/INTERACTION.md).

## 5c. Standing services (dispatch + dashboard)

The oneshot unit above runs one job and exits. A server that accepts jobs
*remotely* and shows them in a browser instead runs **two long-lived units**
that share one workspace: the dispatch service **writes** runs into it, the
dashboard **reads** them back out.

### Dispatch — the remote job runner

[`deploy/dev-team-dispatch.service`](deploy/dev-team-dispatch.service) is a
hardened singleton that serves the authenticated HTTP API (`--dispatch`):
submit / poll / fetch **assess**, **deliver**, and **verify** jobs, run one at
a time (single-flight, since the box has one shared Claude subscription). It
binds the **tailnet IP only** — never the public internet, because it holds
Claude credentials and executes agent code — and authenticates every route
except `GET /health` with a bearer token. Full API in
[`docs/DISPATCH.md`](docs/DISPATCH.md).

```bash
sudo cp deploy/dev-team-dispatch.service /etc/systemd/system/
sudo mkdir -p /etc/dev-team /opt/dev-team/workspace
sudo chown devteam:devteam /opt/dev-team/workspace

sudo tee /etc/dev-team/dev-team.env >/dev/null <<'EOF'
CLAUDE_CODE_OAUTH_TOKEN=<token from claude setup-token>
# ...or instead: ANTHROPIC_API_KEY=sk-ant-...
DEV_TEAM_DISPATCH_TOKEN=<32-byte hex, e.g. from: openssl rand -hex 32>
# GITHUB_TOKEN=github_pat_...   # only to clone private repos
EOF
sudo chmod 600 /etc/dev-team/dev-team.env

sudo systemctl daemon-reload
sudo systemctl enable --now dev-team-dispatch.service
```

Its `ExecStart` passes `--dashboard-workspace /opt/dev-team/workspace`, so
although each job runs in its own isolated clone under `/opt/dev-team/jobs/<id>`,
it **also** journals its events into that shared workspace and mirrors each
assessment's report and structured result there. That shared workspace is the
hand-off to the dashboard. (The same env-file gotcha as 5b applies: keep every
value on its own line, with no trailing inline `#` comment. See
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) if you hit this after a
service is already running.)

### Dashboard — the read-only viewer

[`deploy/dev-team-dashboard.service`](deploy/dev-team-dashboard.service) serves
the web dashboard (`--dashboard`) over that **same** `/opt/dev-team/workspace`.
It is a **separate, read-only process** — it renders the journal, backlog,
memory, conventions, and reports but never writes them — and it runs **no
agents**, so its unit deliberately holds **no Claude credentials**: only
`DEV_TEAM_DASHBOARD_TOKEN`, in its own `/etc/dev-team/dashboard.env`. Full
panel list in [`docs/DASHBOARD.md`](docs/DASHBOARD.md).

```bash
sudo cp deploy/dev-team-dashboard.service /etc/systemd/system/

sudo tee /etc/dev-team/dashboard.env >/dev/null <<'EOF'
DEV_TEAM_DASHBOARD_TOKEN=<url-safe token, e.g. from: python -c 'import secrets; print(secrets.token_urlsafe(32))'>
EOF
sudo chmod 600 /etc/dev-team/dashboard.env

sudo systemctl daemon-reload
sudo systemctl enable --now dev-team-dashboard.service
# http://127.0.0.1:8737/  (bound to loopback — reach it over an SSH port-forward)
```

Set a token even on the loopback bind: a standing service on a shared host is
reachable by anyone with local or SSH access, and the dashboard exposes the
assessment reports (and, with transcripts enabled, raw repo content). The
tokenless "open" mode is for localhost development only.

### How the pair fits together

```
 dispatch (:8738, tailnet)  ──writes──▶  /opt/dev-team/workspace  ◀──reads──  dashboard (:8737, loopback)
   holds Claude creds +                   shared journal, reports,             holds only
   DEV_TEAM_DISPATCH_TOKEN                 and backlog                          DEV_TEAM_DASHBOARD_TOKEN
```

Two processes, two tokens, two env files — one shared workspace. The dashboard
starts **read-only**: the Kanban board's edit controls answer `501` until you
also hand the dashboard the dispatch bearer token. To enable board editing,
add `--dispatch-url http://127.0.0.1:8738` to the dashboard unit's `ExecStart`
and `DEV_TEAM_DISPATCH_TOKEN=...` to `dashboard.env`; edits then proxy to the
dispatch service, which owns every backlog write (see
[`docs/DASHBOARD.md`](docs/DASHBOARD.md)).

## 5d. Sandboxing the whole process (unattended / untrusted runs)

dev-team executes code from the repositories it works on — and that happens on
**two** surfaces:

1. The commands the engine itself runs (delivery gates, the assessment build
   probe). `--sandbox` boxes these per-command in a container (rootless, no
   network, dropped caps, resource limits — see
   [`docs/SANDBOX.md`](docs/SANDBOX.md)). Prefer it whenever a runtime container
   engine is available.
2. The **agentic engineer's own tool loop** — its `Bash`/`Edit` tools run via
   the Claude CLI *on the host*, outside any `CommandRunner`, so `--sandbox`
   cannot reach them. Containing that surface means running the **whole
   dev-team process** in a box.

So for unattended runs, or any run over a repository you don't fully trust, run
the process itself inside a container (or a disposable VM) with no ambient
credentials, tight resource limits, and egress restricted to only what it
needs. The included image already runs as the unprivileged `devteam` user; add
the runtime hardening:

```bash
docker run --rm \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 1024 \
    --memory 4g --cpus 2 \
    --read-only --tmpfs /tmp --tmpfs /home/devteam \
    -e CLAUDE_CODE_OAUTH_TOKEN \
    -v "$PWD/build:/build" \
    dev-team:latest "Health endpoint" "Add a /health endpoint" \
    --deliver --workspace /build --budget-usd 5.0 --json
```

- **Network.** The process needs outbound HTTPS to the Anthropic API (and to
  GitHub when you use `--repo`); it does **not** need inbound or any other
  egress. Don't use `--network none` here — it would break the agents. Instead
  restrict egress to those hosts with a firewall/proxy or a locked-down Docker
  network. (The per-command `--sandbox` container *does* default to no network,
  because gate/test code shouldn't phone home.)
- **Credentials.** Pass only the one credential the run needs (a subscription
  token or API key, and a fine-grained read-only `GITHUB_TOKEN` for private
  `--repo`). Nothing else should be on disk or in the environment — a run over
  an untrusted repo is untrusted-code execution with your Claude account
  attached.
- **Disposability.** Treat the workspace and the container as single-use; the
  `--rm` above discards the container, and a fresh `build/` per run keeps one
  job's artifacts out of the next.

This outer box is the belt; `--sandbox` is the suspenders. Together they contain
both code-execution surfaces; on a host with no container engine at all, the
disposable-VM form of the outer box is the minimum for untrusted repositories.

## 6. Security notes

- The unit runs as the unprivileged `devteam` user with a hardened sandbox
  (`ProtectSystem=strict`, `NoNewPrivileges=yes`, private tmp).
- Keep credentials (`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`) in the
  root-owned `*.env` files (`chmod 600`), not in the unit or the repo. A
  subscription token is a credential for your whole Claude account — treat it
  like a password and regenerate it (`claude setup-token`) if exposed.
- For `--repo`, keep the GitHub token in an env file rather than exporting
  it: dev-team reads the file itself, hands the credential to git
  per-command, and never places it in the process environment — so gates,
  build probes, and the code under audit cannot read it. Configure it once;
  every run finds it automatically (`./.env`, then
  `~/.config/dev-team/dev-team.env`, then `/etc/dev-team/dev-team.env` —
  the natural home on a server; `--env-file` overrides). Use a fine-grained
  PAT with read-only **Contents** permission scoped to the repositories you
  actually audit, not a classic `repo`-scope token.
- The agents run the Claude CLI in `acceptEdits` mode by default, with tools
  granted per call via `allowed_tools`. `bypassPermissions` is opt-in via
  `TeamConfig`; only enable it inside a sandboxed container/VM.
- Delivery runs (`--deliver`) and the assessment build probe (`--build-probe`)
  execute the code the agents write or the repo ships (that is what running the
  quality gates / probe means), and the agentic engineer's own `Bash` tool runs
  arbitrary commands too. Contain **both** surfaces: `--sandbox` boxes the
  gate/probe commands per-command (see [`docs/SANDBOX.md`](docs/SANDBOX.md)), and
  running the whole process in a container/VM boxes the engineer's tool loop and
  everything else — see [5d](#5d-sandboxing-the-whole-process-unattended--untrusted-runs).
  Treat the host as untrusted-code execution: no ambient credentials, egress
  restricted to the API/GitHub, disposable machine.
