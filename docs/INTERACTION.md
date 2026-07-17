# Interacting with the team

By default a dev-team run is autonomous: you hand it a feature request and
read the report. This document covers the three ways to get *into the loop* —
interactive runs, chat, and the integration surface for building your own
front end — plus the persona system that gives every agent a name.

## Interactive runs (`--interactive` / `-i`)

```bash
dev-team "Password reset" "Let users reset their password via email" -i -v
```

An interactive run pauses at the moments a human actually wants a say:

1. **Plan review** — after the product manager plans the work (and the plan
   passes INVEST lint), the plan is shown with every task's acceptance
   criteria:

   ```text
   Priya asks: Approve this plan (3 task(s))?
   [approve] start the work  [revise] request changes  [abort] stop the run >
   ```

   `revise` takes free-text feedback ("split task 2, drop the migration")
   and loops until you approve; `abort` stops the run before any work (or
   spend beyond planning) happens.

2. **Task-failure escalation** — a task that exhausts its attempts asks
   instead of silently failing:

   ```text
   Sam asks: Task T2 failed all attempts. What now?
   [skip] accept the failure and continue  [retry] retry with your guidance >
   ```

   `retry` takes guidance ("use the existing session helper, don't add a
   dependency") which is fed to the engineer as review feedback for a fresh
   round of attempts.

3. **Approvals** — the feature commit, and any command the side-effect
   policy gates (`push`, `deploy`, `rm`), ask for a yes/no with the risk and
   detail shown.

Every prompt shows a default first, for a present human who just presses
enter — but a closed stdin does **not** silently take that default. A
detached interactive run (piped, `nohup`, a CI or oneshot unit with no
terminal) **fails closed**: on EOF each question falls back to its *fail-safe*
choice, not its default. Plan review aborts before any work (or spend beyond
planning) starts, and every approval — the feature commit and each gated
`push`/`deploy`/`rm` — is denied. So a run that loses its human never
degrades to autonomous and never crashes on the missing input; it stops.
(The one prompt whose fail-safe *is* its default is failed-task escalation:
it falls back to `skip`, leaving the task failed rather than retrying blind.)
On resume from a checkpoint the plan is *not* re-reviewed: it was approved by
the run that created it, and the banked work matches it.

## Supervising CI fixes from the pull request (`--interactive-pr-comments`)

Every question above fires on this terminal, because it happens *before* a
PR exists. Once `--deliver --pull-request --watch-checks --watch-fix-rounds N
--interactive` is running unattended on a server, the one question that fires
*after* the PR is open — "CI is failing, fix it and re-push?" (each
`--watch-fix-rounds` round) — has nowhere to go: nobody is attached to that
terminal. `--interactive-pr-comments` moves just that question onto the PR
itself:

```bash
dev-team "Feature" "..." --deliver --repo acme/mono -i \
  --pull-request --watch-checks --watch-fix-rounds 3 \
  --interactive-pr-comments \
  --interactive-pr-comment-author ada --interactive-pr-comment-author grace
```

The question, its CI-failure context, and the reply menu are posted as a PR
comment; the run polls for a comment from an **explicitly allow-listed**
GitHub login (`--interactive-pr-comment-author`, repeatable — there is no
implicit default, e.g. "the PR author") whose first word is `apply` or
`skip`. Every other comment — an unauthorized login, an unrecognised reply —
is silently ignored. If nothing authorized arrives before the poll bound is
exhausted, the round fails safe to `skip`, exactly like a detached terminal's
EOF behaviour: the fix is never force-pushed without a blessed reply. This
touches only the CI-fix loop's channel — plan review, task-failure
escalation, and approvals still go through `team.interaction` (this
terminal) unchanged, because those questions fire before any PR exists.

**Before enabling this, weigh the exposure change:** the CI-failure summary
that this posts is the same Restricted-classified content `ci_fix_question`
always carried, but today it is seen only on this private terminal (or,
through dispatch's `--interactive` job API, behind a bearer token). Posting
it as a plain PR comment makes it **world-readable on a public repo** — CI
logs can leak more than a red/green status (file paths, partial stack
traces, sometimes fragments of test data). Only enable
`--interactive-pr-comments` on a repository where that diagnostic detail is
fine to be public, or where the repo itself is private.

Requires `--interactive`, `--pull-request`, and `--watch-fix-rounds > 0` (the
flags that already need to be true for a CI-fix round to exist at all).

## Chat (`--chat`)

```bash
dev-team --chat
```

Chat mode opens a conversation with the product manager persona *before*
anything is planned. Unlike the run-time agents (one fresh session per call),
the chat holds a single persistent session, so it remembers the whole
conversation:

```text
chatting with Priya — describe the feature you want (/help for commands)
you > we keep getting support tickets about lost passwords
Priya > That sounds like a password-reset flow. A few questions: email-based
reset links, or do you also need SMS? Should links expire?
you > email only, links expire after an hour
you > /run
handing off to the team (simulation): Password reset — ...
```

Commands: `/run` (simulation), `/deliver` (real delivery — honours all the
usual `--deliver` flags like `--workspace` and `--budget-usd`), `/help`,
`/quit`. When you hand off, the PM distils the conversation into the same
`FeatureRequest` the CLI positionals would have built — title, description,
constraints — and the run proceeds exactly as usual (including plan review,
if you also passed `-i`). After the run you are back in the conversation, so
you can iterate.

## Personas

Every agent has a name and a professional identity, shown in progress events
(`[Priya (product-manager)/planning] Plan ready`), interactive prompts, and
the chat. The default cast:

| Role | Name | Identity |
|------|------|----------|
| product-manager | Priya | pragmatic delivery lead |
| architect | Anders | boring-technology systems thinker |
| engineer | Sam | reads before writing, small diffs |
| reviewer | Rey | calm, evidence-first |
| qa | Quinn | trusts failing tests over promises |
| security-engineer | Sasha | threat models and blast radii |
| technical-writer | Wren | examples over adjectives |
| sre | Riley | assumes things fail |
| devops | Devon | automation with tested rollback |

Personas are **presentation and temperament, never identity**: everything
internal (event `role` fields, checkpoints, memory, commit messages) stays
keyed by role, so renaming the cast can never break a resume, and persona
text is additive — the role's contract (JSON-only responses, evidence
requirements) always survives intact.

Customise with a JSON overlay (unknown roles are rejected loudly):

```bash
cat > roster.json <<'EOF'
{
  "engineer": {"name": "Ada", "style": "You are terse and allergic to cleverness."},
  "reviewer": {"name": "Grace"}
}
EOF
dev-team "Feature" "..." --roster roster.json
```

Or turn them off with `--no-personas`. A word of caution from the research on
persona prompting: identity-level styles (background, communication) are
safe, but temperament that could bias judgement — an extra-lenient reviewer,
an alarmist security engineer — measurably shifts verdicts. That is a
legitimate tuning knob, but use it deliberately.

## Building your own front end

The interactive machinery is a small, UI-agnostic protocol in
`dev_team.interaction`, designed so a web dashboard, TUI, or chat-ops bot can
drive a run without the engine knowing:

- **Events out**: pass a `listener` — every agent and engine step emits an
  `AgentEvent` (`role`, `stage`, `message`, `detail`, persona `name`).
- **Questions in**: pass an `interaction` channel. `QueueChannel` is the
  integration point: the run blocks on `ask()` while your UI services
  `channel.questions` / `channel.replies` from its own thread or event loop
  (with an optional timeout falling back to the default answer, so a dead UI
  never wedges a run).

```python
from dev_team import DevTeam, QueueChannel, Reply

channel = QueueChannel(timeout=3600)
team = DevTeam(listener=my_event_sink, interaction=channel)

# elsewhere — a web handler, Slack bot, TUI loop:
question = channel.questions.get()      # render question.context + choices
channel.replies.put(Reply(choice="approve"))
```

`ScriptedChannel` (canned replies) is the test double; `AutoChannel` answers
every question with its default, which is exactly the autonomous behaviour.

For yes/no-only integration, `ChannelApprovalGate` adapts any channel to the
`ApprovalGate` protocol used by the commit gate and the guarded command
runner.
