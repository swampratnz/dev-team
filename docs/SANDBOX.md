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
| Gate commands (the verify command / agent-authored tests) | ✅ when wired in |
| Build probe / setup / dependency-scan commands | ✅ when wired in |
| git porcelain (commit, branch, worktree, `git log`) | ❌ by design — host |
| The agentic engineer's own SDK tool loop (Bash/Edit via the Claude CLI) | ❌ — see below |

The engineer's tool loop runs via the Claude CLI **on the host**, outside any
`CommandRunner`, so this primitive cannot box it. Containing *that* means running
the whole dev-team process inside a container/VM (a deployment concern, phase
(c)). Until then, for unattended or untrusted runs, put the whole process in a
sandboxed container with no credentials and restricted network — exactly as the
README's *Safety* section already advises.

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

This PR ships the primitive and its tests. Still to come (see ROADMAP item 1):

- **(b)** wire it into the delivery gates and the assessment `--build-probe`
  behind a `--sandbox` opt-in — as a gate/probe runner kept distinct from the
  host git runner;
- **(c)** deployment wiring so the engineer's SDK tool loop runs in the box too.
