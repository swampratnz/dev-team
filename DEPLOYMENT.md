# Deploying dev-team on Ubuntu

`dev-team` targets modern Ubuntu LTS (22.04 / 24.04). It is a Python
application built on the Claude Agent SDK, which in turn shells out to the
Claude Code CLI, so a deployment host needs Python, Node.js (for the CLI), and
a configured `ANTHROPIC_API_KEY`.

## 1. Prerequisites

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nodejs npm
# The Agent SDK drives the Claude Code CLI:
sudo npm install -g @anthropic-ai/claude-code
```

Provide credentials via the environment (never commit them):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## 2. Install the application

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

## 3. Run the test suite on the host (optional but recommended)

```bash
sudo -u devteam bash -c '
  cd /path/to/checkout
  . /opt/dev-team/.venv/bin/activate
  pip install -e ".[test]"
  pytest
'
```

The suite is hermetic (no network) and must report **100% coverage**.

## 4a. Run as a container

A `Dockerfile` is included:

```bash
docker build -t dev-team:latest .
docker run --rm -e ANTHROPIC_API_KEY \
    dev-team:latest "Health endpoint" "Add a /health endpoint" --json
```

## 4b. Run as a systemd unit

`dev-team` is a task runner rather than a daemon, so it is deployed as a
**templated oneshot unit** you start on demand or from a timer. The unit file
is provided at [`deploy/dev-team@.service`](deploy/dev-team@.service) and reads
its arguments from an environment file.

```bash
sudo cp deploy/dev-team@.service /etc/systemd/system/
sudo mkdir -p /etc/dev-team

# One environment file per job, named to match the instance:
sudo tee /etc/dev-team/health.env >/dev/null <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
DEV_TEAM_TITLE=Health endpoint
DEV_TEAM_DESCRIPTION=Add a /health endpoint that returns 200 OK
DEV_TEAM_ARGS=--json
EOF

sudo systemctl daemon-reload
sudo systemctl start dev-team@health.service
journalctl -u dev-team@health.service -f
```

To run it on a schedule, pair the service with a systemd timer
(`dev-team@health.timer`).

## 5. Security notes

- The unit runs as the unprivileged `devteam` user with a hardened sandbox
  (`ProtectSystem=strict`, `NoNewPrivileges=yes`, private tmp).
- Keep `ANTHROPIC_API_KEY` in the root-owned `*.env` files (`chmod 600`), not in
  the unit or the repo.
- The agents run the Claude CLI in `bypassPermissions` mode by default; scope
  the host and the working directory accordingly, or set a stricter
  `permission_mode` via `TeamConfig`.
