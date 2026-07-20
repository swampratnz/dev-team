# Sandboxing untrusted code (`dev_team.sandbox`)

dev-team's day job — running a project's own build/test commands, or an
agent-authored test suite — is **arbitrary code execution**. The argv-level
[`SideEffectPolicy`](../src/dev_team/policy.py) is defence-in-depth, not
containment: nothing at the argv layer stops code a gate *runs* from reading the
host filesystem, exfiltrating over the network, or spawning further processes.

`ContainerCommandRunner` is the containment layer. It is the first slice of
ROADMAP item 1 (see [`ROADMAP.md`](ROADMAP.md)).

## What it does

It is a `CommandRunner` (the same tiny protocol the engine already runs gates
through) that wraps **another** `CommandRunner` and, for each command:

- **delegates git to the host, unchanged.** git porcelain
  (`commit`/`branch`/`stash`/`worktree`/`log`) is orchestration dev-team itself
  controls, not untrusted code, and it must act on the real repository. Any
  program in `SandboxConfig.host_programs` (just `git` by default) runs on the
  host.
- **boxes everything else in a container.** Every other command is rewritten
  into a `docker`/`podman run` invocation and handed to the inner runner to
  execute. By default that container has:
  - **no network** (`--network none`) — no exfiltration, no dependency
    confusion;
  - **all capabilities dropped** (`--cap-drop ALL`) and
    **no-new-privileges**;
  - **resource ceilings** (`--memory 2g`, `--cpus 2`, `--pids-limit 512` — the
    last a fork-bomb backstop);
  - **only the workspace mounted** — `cwd` (which the engine roots at the
    workspace or a per-task worktree) is bind-mounted to `/workspace` and is the
    single writable, shared surface; nothing else on the host is visible;
  - **only the caller-supplied `env`** forwarded — never the host environment.
    It rides in via a mode-`0600`, workspace-external, always-deleted
    `--env-file` (never inline `--env KEY=VALUE`), so a credential passed
    through it never lands in the container CLI's argv (`ps` /
    `CommandResult.command` / an audit report) and the boxed code can't read the
    file either.

Because it only *builds* the container argv and delegates execution to the inner
runner, it is fully testable without a container engine, and it inherits the
inner runner's timeout handling and secret-env scrubbing.

## Trust boundary vs. isolation boundary

| Surface | Contained by this? |
|---|---|
| Gate commands (the verify command / agent-authored tests) | ✅ via `--sandbox` |
| Build probe / setup / dependency-scan commands | ✅ via `--sandbox` |
| git porcelain (commit, branch, worktree, `git log`) | ❌ by design — host |
| The agentic engineer's own SDK tool loop (Bash/Edit via the Claude CLI) | ❌ not by this primitive — needs the outer process container/VM (§5d), see below |
| Visual review's served app (`SubprocessAppServer`) | ❌ not by `--sandbox` — long-running and needs inbound access from the host's Playwright capturer, a different shape than the boxed one-shot `CommandRunner` contract; the engine logs an advisory warning instead of silently assuming coverage (see `DeliveryEngine._visual_review`) |

The engineer's tool loop runs via the Claude CLI **on the host**, outside any
`CommandRunner`, so this primitive cannot box it. Containing *that* means running
the whole dev-team process inside a container/VM — the phase (c) deployment
model, documented with a hardened recipe in
[`DEPLOYMENT.md` §5d](../DEPLOYMENT.md#5d-sandboxing-the-whole-process-unattended--untrusted-runs).
`--sandbox` is the per-command belt; the outer process container is the
suspenders that also covers the engineer's tool loop and everything else.

## Configuration

```python
from dev_team import ContainerCommandRunner, SandboxConfig, SubprocessCommandRunner

runner = ContainerCommandRunner(
    SubprocessCommandRunner(),                 # the host runner it wraps
    SandboxConfig(
        engine="podman",                       # rootless by default
        image="python:3.12-slim",              # must carry your toolchain
        network="none",                        # override for a setup that fetches deps
        memory="2g", cpus="2", pids_limit=512,
        read_only_rootfs=False,                # opt-in; pair with tmpfs when your stack tolerates it
    ),
)
result = runner.run(["pytest", "-q"], cwd="/path/to/workspace")
```

### From the CLI

`--sandbox` boxes the commands a `--deliver` or `--assess` run executes (the
delivery gates, the assessment build probe); git stays on the host.

```bash
# gate the delivery in a container (rootless docker/podman required at runtime)
dev-team "Health endpoint" "Add /health returning 200" --deliver --workspace ./build \
    --sandbox --sandbox-image python:3.12-slim

# a build probe that must restore dependencies needs a network:
dev-team --assess --workspace /path/to/repo --build-probe \
    --sandbox --sandbox-network bridge --sandbox-engine podman
```

`--sandbox-image` / `--sandbox-network` / `--sandbox-engine` override the
matching `SandboxConfig` field; everything else keeps its secure default.

`--dispatch --sandbox` boxes every dispatched job's gates/build-probe the
same way `--deliver`/`--assess --sandbox` do — see
[`DISPATCH.md`](DISPATCH.md#sandboxing) for the server-start-time-only
posture (not a per-request `POST /jobs` option).

Notes and gotchas:

- **Rootless is the model.** `user` is unset by default; a rootless engine
  (rootless podman, or a rootless docker daemon) maps the container to the
  invoking host user, so files written into the mounted workspace keep correct
  ownership. Set `user="UID:GID"` for a root docker daemon.
- **Pick an image with your toolchain.** There is no universally correct
  default; the default is a small Python base. A .NET or Node gate needs the
  matching SDK in the image.
- **`network="none"` blocks dependency restore.** A test command usually needs
  no network, but `npm install` / `dotnet restore` / `pip install` do — give the
  setup step an override (or a pre-provisioned image) rather than opening the
  network for the tests.
- **`read_only_rootfs` is opt-in.** Many toolchains write to `$HOME`/caches
  outside the workspace; enable it with `tmpfs=("/tmp",)` (and a workspace
  `$HOME`) only when your stack tolerates it.

## Status

- **(a) primitive** — shipped: `ContainerCommandRunner` + `SandboxConfig`.
- **(b) wiring** — shipped: `EngineConfig.sandbox` / `AssessConfig.sandbox` and
  the `--sandbox` CLI opt-in box the delivery gates and the assessment build
  probe, for `--deliver`/`--assess` **and** `--dispatch` (`--dispatch
  --sandbox` boxes every dispatched job's gates/build-probe the same way, as
  a server-start-time operator choice — see
  [`DISPATCH.md`](DISPATCH.md#sandboxing)). Because git self-delegates to the
  host inside the runner, no separate gate/git runner split was needed. Two
  guardrails from review are in place: the program name reaching the runner
  is engine/profile-controlled (never repo-derived, so a repo script named
  `git` cannot slip onto the host), and the mount source is
  `realpath`-resolved.
- **(c) process-level** — shipped as deployment guidance: run the whole
  dev-team process in a container/VM to contain the engineer's own SDK tool loop
  (which bypasses the `CommandRunner`). A hardened `docker run` recipe and the
  layered model live in
  [`DEPLOYMENT.md` §5d](../DEPLOYMENT.md#5d-sandboxing-the-whole-process-unattended--untrusted-runs);
  the standing systemd units carry matching hardening (the read-only dashboard
  unit fully locked down; the job-running units kept namespace/syscall-open so
  the in-process sandbox's container engine still works).
