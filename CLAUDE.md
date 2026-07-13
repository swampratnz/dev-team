# Engineering Standards for AI-Assisted Development

This repo builds and operates AI agents whose output reaches products, systems, and repositories. These standards therefore apply in full. They encode Secure Development Policy, Information Security Policy (AUP), AI Agent IAM Policy, and AI Use and Governance Policy into working rules for building tooling with Claude Code.

**Precedence:** These rules win over convenience, speed, or a cleaner diff. Where a rule and a request conflict, follow the rule and say so. Where a situation is not covered or is ambiguous, stop and ask rather than guess. Scope is set by the destination of the code, not by who wrote it or what it was originally for: if the output reaches a BPG product, system, or infrastructure, or processes customer, personal, or confidential data, these standards apply in full.

## 0. Classify before you build

Before writing code, establish two things and state them back:
1. **Data classification** of everything the code and the session will touch. Confidential covers source code, customer data, personal data, credentials and secrets, financial, salary and strategic information, and vulnerability or litigation material. Restricted is the default for internal information. Public is everything cleared for release.
2. **Tier.** Lightweight if internal, non production, non confidential. Full if destined for production, or it processes customer, personal, or confidential data, or it touches production infrastructure. Full tier applies every gate below.

Never enter Confidential or Restricted data into an AI tool or account that is not approved for it in the Application Catalogue. A personally owned or consumer account may receive Public data only, and must never ingest a repository, customer or personal data, or secrets.

## 1. Accountability and human oversight

Every tool, feature, or agent has one named human owner accountable for its behaviour. The developer is accountable for AI-assisted code as if they wrote every line by hand. AI does not make consequential decisions on its own: anything touching security, access, people, money, legal matters, or customers requires a person to make the call. Treat all model output as a draft to verify, not an authority.

## 2. Secrets and credentials

Never place a secret in source code, in committed config or env files, in build or application logs, in error output, in client-side code, or in an AI prompt or agent context. Store application secrets (API keys, signing keys, connection strings, OAuth client secrets, service tokens) in the approved secrets manager (Azure Key Vault). Scope every secret to the minimum permission required and assume rotation. Treat any secret committed to version control as compromised: rotate it, do not just delete the commit. Run automated secret scanning on the repo and include a credential check in review.

## 3. Least privilege by default

Grant the narrowest scope that makes the function work. Never request or use administrative or owner-level scopes where a narrower one is achievable. For Snowflake, agent and service roles are custom roles, never SYSADMIN, ACCOUNTADMIN, or SECURITYADMIN. Apply least privilege to file access, network egress, database grants, and API scopes alike.

## 4. Secure and private by design

Minimise attack surface. Establish secure defaults. Fail securely and do not leak internals in errors. Do not trust upstream services or their output. Keep security simple and fix root causes, not symptoms. Privacy is the default, not an add-on: collect and retain the minimum, classify stored data by the classification of its source, and design opt-out and deletion in from the start rather than bolting them on.

## 5. AI-assisted code quality

Confirm that every dependency an AI tool suggests actually exists and is the intended, legitimate package before adding it. Guard against hallucinated and look-alike (slopsquatted) packages. Code that is security sensitive (authentication, cryptography, access control, or handling of personal or customer data) gets senior or security review before merge, whether or not AI wrote it. All code is version controlled. No single person develops, tests, and deploys a change without approval and oversight.

## 6. Building AI and agentic features

Treat prompt injection, direct and indirect (via retrieved documents, tool output, or any external content), as a first-class attack vector from design. Isolate system prompts and instructions from user-supplied and third-party content. Treat model output as untrusted input: sanitise anything rendered to users, and validate and constrain anything that triggers a downstream action (API call, database write, file or system operation, tool or function call) to least privilege with an explicit authorisation check. Never execute model output as code or as a privileged operation without that check. Treat AI memory and retrieval features as an injection and data-leakage surface, and carry the source data's classification into stored context. Classify data before sending it to any third-party model provider, and only send Confidential data to an approved provider. Log prompts and responses for audit and incident response.

## 7. Agent identity and runtime credentials

If the tool acts autonomously against systems, it is an agent and the AI Agent IAM Policy applies. No agent holds credentials in its runtime context. Authenticate only through an approved pattern: Workload Identity Federation via Entra (Pattern A), IdP-delegated OAuth (Pattern B), or Credential Proxy Injection from Key Vault (Pattern C). Agents use their own distinct identity, never a human's credentials, delegated token, or personal OAuth session. Register every agent in the Agent Registry before it calls production or staging, with a named business owner, its permission scope and justification, and its authorised tool servers. Connect only to tool servers on the VP Security allowlist. Tool servers reaching finance, HR, or customer data need VP Security sign-off. Every agent authentication and call must produce a retained, reviewable log; a log gap is a control failure.

## 8. Environments, testing, and release

Keep production, test or staging, and development logically or physically separate. Do not use customer data for testing without the business data owner's and the VP of Development or VP of Platform's permission. No code reaches production without documented, successful security test results and evidence that findings were remediated. Scan application code before deployment and remediate materially security-relevant vulnerabilities within 90 days. Complete the Release Checklist, including all test plans, before deploying.

## 9. When to stop and escalate

Stop and raise it rather than proceed if any of these are true: the work would put Confidential or Restricted data into a non-approved or personal account or tool; a required credential is not available through Key Vault or an approved pattern; an agent would need a scope or a tool server that is not approved; you cannot produce audit logs for what the code does; or the request conflicts with any rule above. Route tool or access requests to ISD, and security or privacy concerns to chris @ watson.geek .nz . Exceptions to the Secure Development Policy go to the Head of Infrastructure and Security with CTO approval.

## Definition of done

Data classification and tier stated. No secrets in code, config, logs, or context. Least privilege applied. Dependencies verified. Security-sensitive code reviewed. Prompt-injection and untrusted-output handling in place for any AI feature. Agents registered with an approved auth pattern and allowlisted tool servers. Tests passed and evidenced. Release Checklist complete. A named owner recorded.

## Multi-loop pipeline

This repo is developed by a supervised multi-session pipeline — see
`docs/PIPELINE.md`. If you are running as one of those loops, obey the
ownership rules:

- **Only the build loop** writes code or opens PRs. PR-review comments only;
  research & adversarial touch issues only. One exception: the **autofix
  loop** (`pipeline-pr-autofix.yml`) may push fixes to an existing
  build-worker PR branch when its CI fails — bounded to 2 attempts; same-repo
  bot PRs with a `Closes #` body only (unrelated bot PRs like Dependabot
  bumps and PRs already labelled `needs-human` are skipped); and only from CI
  `run_attempt` ≥ 2 (the ci-retry loop below gets one free machine rerun
  first, so agents never chase one-off flakes), then it escalates
  `needs-human`. Before assuming a code defect it checks for a flaky,
  unrelated test (re-run in isolation with `--no-cov` — a partial run can
  never satisfy the global coverage gate — and if it passes there, CI is
  re-triggered with an empty commit instead of pushing a bogus "fix"). It
  never opens or merges PRs. Do not misflag its pushes as an ownership
  violation.
- The **conflict-resolver loop** (`pipeline-pr-conflict.yml`) may push a
  `main`-merge to an existing PR branch when that PR is CONFLICTING. It is
  two-hop: a `discover` job (on every push to `main`, on PR
  opened/ready-for-review — a PR can be *born* conflicted — and on an hourly
  backstop sweep) finds conflicting same-repo PRs and self-dispatches the
  `resolve` job via `workflow_dispatch`, because claude-code-action won't run
  under a `push` event. The dispatch payload carries PR **numbers only**;
  resolve re-derives the branch from the API and re-verifies the full
  eligibility contract before checkout: same-repo (never a fork), not
  `needs-human`/`no-auto-resolve`, still CONFLICTING, and **either** a bot PR
  with `Closes #` **or** a maintainer PR whose author is in the
  `MAINTAINER_LOGINS` allowlist. One attempt per conflict: a failed
  resolution escalates `needs-human`, and the eligibility filter skips
  `needs-human` PRs so it never thrashes. Same push guardrails as autofix
  (read-only `gh`, exact `git push origin HEAD`). It never opens or merges
  PRs. Do not misflag its merge commits as an ownership violation either.
- The **revise loop** (`pipeline-pr-revise.yml`) may push review-response
  commits to an existing build-worker PR branch when the PR-review worker's
  verdict is "Changes requested" — the green-CI case autofix (CI-failure
  keyed) never touches. Two-hop like the conflict resolver: the review
  workflow's post step self-dispatches it via `workflow_dispatch` (a
  GITHUB_TOKEN-posted comment can never trigger a workflow), the payload
  carries the PR number only, and eligibility plus the still-pending verdict
  are re-verified from the API before checkout. Bounded to 2 attempts per PR
  via marker comments, then it escalates `needs-human`; a "Needs a human
  decision" verdict labels `needs-human` directly from the review workflow.
  It never opens or merges PRs. Do not misflag its pushes as an ownership
  violation either.
- The **build-retry loop** (`pipeline-build-retry.yml`) auto-re-runs a build
  worker run that failed to produce a PR, via `gh run rerun`, bounded by
  `run_attempt` (≤3 total attempts). The build worker escalates `needs-human`
  only on its final attempt, so transient/infra failures recover unattended
  and a human is pinged only for persistent ones — don't re-add manual
  re-trigger steps for build failures.
- The **ci-retry loop** (`ci-retry.yml`) gives a failed CI run one blind
  machine rerun (`gh run rerun --failed`, `run_attempt` < 2) before any agent
  engages. It holds `actions: write` only, touches no code, and hands off to
  autofix from attempt 2.
- The build worker runs the **full CI gate** (`ruff check .` and `pytest`
  with the 100% branch-coverage gate, on the same setup-python + editable
  install ci.yml uses) BEFORE opening a PR, so "green locally" matches CI.
  Keep it that way when editing either the pipeline workflows or `ci.yml` —
  they must run the same checks. Never weaken the coverage gate to make a
  build pass.
- **No loop merges PRs — a human merges.** This is the pipeline restatement
  of section 5 above (no single actor develops, tests, and deploys without
  approval and oversight). It is enforced structurally, not just by prompt:
  the workers' `--allowedTools` grant no blanket `git:*`/`gh:*`/`python:*`
  and no form of `gh pr merge` or `gh api`; branch protection on `main` is
  the enforceable backstop and a required repo setting.
- WIP caps: ≤3 open `status:draft`. Builds run **per-issue** (each issue its
  own `concurrency` group, so distinct issues run in parallel and none evicts
  another — a single shared group would silently *cancel* queued builds, and
  cancellations aren't retried). Every run draws on the shared Max pool
  (also serving the sibling community-agent pipeline and the Dave Discord
  bot), so don't release large bursts of approvals at once.
- Coordinate only through issue labels; when blocked or ambiguous, add
  `needs-human` and stop rather than guess.
- Everything traces to an issue number.
