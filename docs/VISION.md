# VISION — what makes dev-team great

The shared north star for the self-improvement pipeline (docs/PIPELINE.md). The
**research** worker generates proposals against this; the **adversarial**
worker judges them against the same bar. Tune quality by editing this file, not
the loop prompts.

## Mission

Make dev-team the most trustworthy way to run **autonomous, supervised SDLC
agents**: a system that assesses repositories, delivers changes, and
adversarially verifies its own findings — safely, reproducibly, and cheaply.
Concretely, dev-team should:

- produce **assessments a maintainer can act on**: cited, phased audits
  (security, risk, conventions, recommendation, dead code, dependency CVEs)
  whose claims survive adversarial re-verification,
- **deliver changes professionally**: green-baseline discipline, curated
  commits, evidence-based review, gates identical to CI,
- stay **operable**: a dispatch API and dashboard the operator (and Dave, the
  Discord front-end) can drive remotely and observe at a glance,
- and do all of it **within budget** — every agent run has a cost story.

All of it under the BPG Engineering Standards (CLAUDE.md): humans merge, least
privilege, no secrets in context, prompt injection treated as a first-class
threat — this system clones and reads UNTRUSTED third-party repositories as its
day job.

## Who we serve

- **The operator** — runs the services, approves proposals, merges PRs.
  Value = control, safety, visibility, and low babysitting effort.
- **Repo owners being assessed/delivered against** — the customers of audit
  reports and delivered PRs. Value = accuracy, actionability, no damage.
- **Dave (the Discord community-agent)** — drives dispatch remotely via
  super-admin tools. Value = a stable, scriptable API surface.

## What a great proposal does

Score each idea on:

1. **Real problem, with evidence** — does it fix something observed (a
   ROADMAP.md deferral, an open issue, an operator complaint, a weak audit
   verdict), or is it invention?
2. **Effort** — shippable in roughly **one PR** (with tests) beats a project.
3. **Security/BPG fit** — respects CLAUDE.md and the existing posture: token
   hygiene, argv-semantic command policy, workspace containment, prompt-
   injection discipline. A feature that fights the design is a bad feature
   even if useful.
4. **Cost story** — states its token/runtime impact per run and why that's
   acceptable on a shared Max pool.
5. **Testable acceptance criteria** — concrete assertions a build worker can
   turn into tests, compatible with the **100% branch-coverage gate** (if you
   can't say how it will be tested to 100%, it isn't ready).

Prefer **high-impact, low-effort, low-risk**. When unsure, propose the
**smallest viable version** and note how it could grow.

## Ground proposals in evidence

Propose from observed need, not imagination. Signal sources (in rough order):

1. `operator-feedback` issues (real operator/user requests).
2. **docs/ROADMAP.md** — the known, deliberately deferred capabilities, already
   prioritised.
3. Open and closed `proposal` issues — build on what's wanted, avoid what was
   rejected (read *why* it was rejected).
4. **CHANGELOG.md** and recent commits — know what already exists so you
   extend, not duplicate.
5. "Intentionally deferred" / "out of scope" notes in merged PR bodies —
   pre-scoped follow-up work someone already judged worthwhile.
6. Gaps called out in docs/ (ASSESSMENT.md, DISPATCH.md, DASHBOARD.md,
   BENCHMARKS.md) — documented residual risks and limitations.

## Theme areas (rotate for diversity)

Each research run is memoryless, so deliberately vary across:

- **Assessment quality** (`theme:assessment`) — better audit phases, new
  phases, citation quality, fewer false positives.
- **Verification depth** (`theme:verification`) — adversarial re-checking,
  refute-first discipline, verdict calibration.
- **Dashboard & Kanban UX** (`theme:dashboard`) — backlog visibility, run
  observability, report browsing.
- **Dispatch & Dave integration** (`theme:dispatch`) — job queue robustness,
  API ergonomics for the Discord front-end.
- **Agent efficiency & token cost** (`theme:efficiency`) — fewer turns, tighter
  prompts, budget handling, doing more within the pool.
- **Security hardening** (`theme:security`) — containment, secret hygiene,
  injection resistance — *within* the existing posture, never relaxing it.
- **Docs & runbooks** (`theme:docs`) — operator docs, deployment runbooks,
  troubleshooting guides.

## Guardrails — do NOT propose

- **Anything granting merge autonomy.** Humans merge all PRs (BPG standard);
  no proposal may weaken, automate, or route around that gate.
- **Credential or secret-handling changes** — new places tokens flow, relaxed
  scrubbing, secrets in new contexts. The current hygiene (env scrubbing,
  per-command git credentials, transcript redaction) is a floor, not a topic.
- **New external service credentials** — features that only work if the system
  is given another API key, webhook secret, or third-party account.
- **Schemes where agents push to `main`** — every loop works on branches
  behind branch protection; that is structural, not stylistic.
- **Multi-PR epics**, rewrites, or sweeping refactors; anything not shippable
  in ~one PR.
- **Weakening the coverage gate** — no lowering `--cov-fail-under`, no blanket
  `pragma: no cover`, no excluding files from coverage to make a feature fit.
- Features that **blow the shared Max pool** (heavy always-on model calls,
  chatty background work) without a cost story.
- **Duplicates** of shipped work or existing proposals; vague "improve X".

A skipped run is better than a weak proposal. Quality over volume.
