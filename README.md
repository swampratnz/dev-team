# dev-team

A **multi-agent software development team** built on the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).

`dev-team` coordinates a roster of role-specialised AI agents — the same roles a
real engineering team has — and drives a feature request through the entire
software development lifecycle: planning, design, implementation, code review,
QA, and deployment planning.

- ✅ **Every role a development team needs**, modelled as an agent.
- ✅ **Built on the Agent SDK** — the Claude Agent SDK is the execution base.
- ✅ **100% test coverage** (branch coverage), enforced in `pyproject.toml`.
- ✅ **Ubuntu-ready** — packaged for deployment as a container or systemd unit.

---

## The team

| Agent | Role | Responsibility |
|-------|------|----------------|
| `ProductManagerAgent` | Product / delivery | Decompose a request into ordered, acceptance-criteria-bearing tasks. |
| `ArchitectAgent` | Architecture | Produce a technical design: components, tech stack, risks. |
| `EngineerAgent` | Engineering | Implement each task, and address review feedback on retries. |
| `ReviewerAgent` | Code review | Approve work or request changes with severities. |
| `QAAgent` | Quality assurance | Design tests and report pass/fail plus coverage. |
| `DevOpsAgent` | DevOps | Produce a deployment plan with steps and rollback, targeting Ubuntu. |

These are orchestrated by the `DevelopmentWorkflow` state machine, wrapped by the
`DevTeam` facade.

## Lifecycle

```
FeatureRequest
     │
     ▼
 ProductManager ──▶ Plan (tasks, dependencies)
     │
     ▼
   Architect  ──▶ Design
     │
     ▼
 for each task (in dependency order):
     ┌───────────────────────────────────────────────┐
     │  Engineer ─▶ Implementation                    │
     │  Reviewer ─▶ Review  ── not approved ─┐        │
     │       │ approved                      │ retry  │
     │       ▼                               │        │
     │  QA ─▶ TestReport ── tests fail ──────┘        │
     │       │ tests pass                             │
     │       ▼                                        │
     │  Task DONE                                     │
     └───────────────────────────────────────────────┘
     │
     ▼
    DevOps  ──▶ DeploymentPlan
     │
     ▼
 ProjectResult
```

A task is retried up to `max_task_attempts` times whenever review or QA rejects
it; the engineer receives the feedback on the next attempt. If it never passes,
the task is marked `FAILED` and the overall run reports incomplete.

## Architecture

The one and only integration point with the Claude Agent SDK is
`dev_team.sdk.ClaudeAgentRunner`, which implements the tiny `AgentRunner`
protocol. Everything above it depends only on that protocol, which is why the
whole system is testable to 100% coverage without spawning the Claude CLI or
making network calls — tests inject a `ScriptedRunner`.

```
cli ─▶ team.DevTeam ─▶ workflow.DevelopmentWorkflow ─▶ agents/* ─▶ sdk.AgentRunner
                                                                     ├─ ClaudeAgentRunner (real SDK)
                                                                     └─ ScriptedRunner (tests)
```

Key modules:

- `models.py` — dataclasses/enums for the whole SDLC (`Task`, `Plan`, `Design`,
  `Implementation`, `Review`, `TestReport`, `DeploymentPlan`, `ProjectResult`).
- `sdk.py` — the Agent SDK adapter and `AgentRunner` protocol.
- `agents/` — one module per role.
- `workflow.py` — the plan → design → (implement/review/test)* → deploy machine.
- `ordering.py` — topological ordering of tasks with cycle detection.
- `json_utils.py` / `parsing.py` — robust extraction of structured data from
  model output.
- `team.py` — the `DevTeam` facade and workflow factory.
- `cli.py` — the `dev-team` command.

## Installation

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[test]'
```

The Claude Agent SDK requires the Claude Code CLI to be available at runtime;
see the [SDK docs](https://github.com/anthropics/claude-agent-sdk-python).

## Usage

### Command line

```bash
dev-team "Password reset" "Let users reset their password via an emailed link" \
    --constraint "must expire links after 1 hour" \
    --verbose
```

Output as JSON for scripting:

```bash
dev-team "Health endpoint" "Add a /health endpoint" --json
```

Exit codes: `0` success, `1` completed with failed tasks, `2` invalid input.

### Library

```python
import asyncio
from dev_team import DevTeam, TeamConfig

async def main():
    team = DevTeam(config=TeamConfig(max_task_attempts=3))
    result = await team.develop_feature(
        "Password reset",
        "Let users reset their password via an emailed link",
        constraints=["links expire after 1 hour"],
    )
    print("success:", result.success)
    for tr in result.task_results:
        print(tr.task.id, tr.task.status.value)

asyncio.run(main())
```

### Bring your own runner

Any object implementing `AgentRunner.run(...)` can back the team — handy for
tests, dry runs, or routing to a different transport:

```python
from dev_team import DevTeam
from dev_team.testing import ScriptedRunner, json_response

runner = ScriptedRunner(by_system_prompt={
    "product manager": json_response({"summary": "...", "tasks": [...]}),
    # ...one entry per role...
})
team = DevTeam(runner)
```

## Testing

```bash
pytest
```

`pytest` is preconfigured (in `pyproject.toml`) to run with branch coverage and
**fail under 100%**. The suite uses only in-memory test doubles, so it is fast
and hermetic.

## Deployment on Ubuntu

See [`DEPLOYMENT.md`](DEPLOYMENT.md) for running `dev-team` on an Ubuntu host,
either as a container or a systemd unit.

## License

MIT
