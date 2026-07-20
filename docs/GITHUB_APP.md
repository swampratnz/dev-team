# GitHub App authentication & user sign-in

Two related capabilities, both optional and both configured through the
same env-file search every other credential uses (`./.env` →
`~/.config/dev-team/dev-team.env` → `/etc/dev-team/dev-team.env`):

1. **GitHub App installation tokens** replace the single long-lived PAT
   for everything the *service and its agents* do against repositories
   (clone, push, PR, checks).
2. **GitHub OAuth sign-in** authenticates *humans* to the dispatch
   service, and decides which repositories each signed-in user may point
   jobs at.

The split is deliberate (CLAUDE.md section 7): agents operate under the
App's **own identity** — never a human's delegated token or personal
OAuth session. The user's OAuth grant is used for exactly two API calls
(identify the user; list their installations) and is not retained.

## 1. App installation tokens (`githubapp.py`)

Register a GitHub App (Settings → Developer settings → GitHub Apps) with
least-privilege permissions — typically **Contents: read & write**,
**Pull requests: read & write**, **Checks: read**, **Metadata: read** —
and install it on the organisations/repositories the service should
reach. Then configure:

```ini
# /etc/dev-team/dev-team.env
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_FILE=/etc/dev-team/app-private-key.pem
```

- The private key sits in a **file** (root-readable-only, like the env
  file that names it), never in an environment variable. Treat it like
  any Confidential credential: approved secret storage, rotation on
  suspicion of exposure.
- Both keys are **popped from the process environment** when found
  there, so gates, build probes, and the code under audit can never read
  them — the same hygiene the PAT path applies.
- Requires the `github` extra: `pip install 'dev-team[github]'` (PyJWT +
  cryptography for the RS256 app JWT). The core package never imports
  them.

How it works: every credential consumer resolves a `TokenProvider`
(`resolve_token_provider`). With the App configured, `token_for(ref)`
signs a short-lived app JWT, resolves the repo's installation, and mints
a **one-hour installation token scoped to that single repository**
(least privilege even when the installation spans an org), cached per
repo and re-minted near expiry — consumers mint at each use, so a
multi-hour delivery never publishes with a dead credential. Without the
App, the classic `GITHUB_TOKEN`/`GH_TOKEN` PAT (or anonymous access)
behaves exactly as before. Non-GitHub hosts always get anonymous access —
App credentials are only ever presented to `github.com`.

Half-configuration (an id without a key file, or vice versa) is a loud
error, never a silent fallback to anonymous clones. A configured-but-
missing App installation fails with "the GitHub App is not installed on
owner/repo — install it first".

## 2. User sign-in on dispatch (`oauth.py`)

Enable the App's OAuth device ("Request user authorization during
installation" is not required; any OAuth credentials on the App or a
separate OAuth app work) and configure:

```ini
GITHUB_OAUTH_CLIENT_ID=Iv1.abc123
GITHUB_OAUTH_CLIENT_SECRET=...
```

When configured, `--dispatch` starts with "GitHub sign-in enabled" and
three routes appear (all 404 when unconfigured — no new surface):

- `GET /auth/login` (no auth) → `{"url", "state"}` — send the user to
  `url`; `state` is a single-use CSRF token (10-minute expiry).
- `GET /auth/callback?code=…&state=…` (no auth) → exchanges the code,
  identifies the user, snapshots the installations their account can
  reach, and answers `{"session_token", "login", "installations",
  "expires"}`. Present the session token as `Authorization: Bearer …`.
- `POST /auth/refresh` (session bearer) → renews the session from the
  stored refresh token; **rotates** the session token and re-snapshots
  installations, so a revoked installation drops out at the next
  refresh.

Session model: opaque random tokens, in memory only (a restart signs
everyone out, matching the in-memory job registry), 8-hour expiry
matching GitHub's user-token lifetime.

### What a session may do

| Capability | Operator token | Signed-in session |
|---|---|---|
| Submit jobs (`POST /jobs`) | any repo, any git host | only **github.com** repos whose **owner is in the user's installation snapshot** (403 otherwise — the host is checked, not just the owner path segment, so a session cannot point the pipeline at `https://internal-git/acme/x`; a verify against a foreign or unknown source job answers the same `404 no assessment` a nonexistent one does, before any disk read, so assessments can't be enumerated) |
| Observe jobs (`GET /jobs`, `/jobs/{id}` status/result/findings/verifications/question) | all jobs | **only jobs on repos in the user's installations** — the listing is filtered, and a foreign tenant's job answers the same `404 unknown job` a nonexistent one does |
| `GET /checks` | any repo | installation-gated like submits |
| Answer interactive questions, cancel queued jobs | ✓ | ✓ for the session's own tenants' jobs (404 otherwise) |
| Cross-tenant aggregates: `GET /backlog`, `/calibration`, `/costs`, `/foreman/plan`, `POST /jobs/{id}/backlog` | ✓ | **403 — operator only** (they aggregate every tenant's data) |
| `POST /foreman/run`, purge/archive/unarchive, backlog mutations, `/access-log` | ✓ | **403 — operator only** |

One dispatch instance can therefore serve unrelated organisations: a
signed-in user can neither read nor probe another tenant's jobs, findings,
costs, or backlog — their world is exactly the repos their App
installations cover.

The operator token remains what it always was: the box's admin
credential in `DEV_TEAM_DISPATCH_TOKEN`.

## 3. Concurrency & cross-repo builds (dispatch)

- `--max-concurrent-jobs N` (default 1 = the classic single-flight
  queue) runs jobs on **distinct** repositories concurrently; two jobs
  on the same repository always serialise (submit order preserved per
  repo — the serialisation key survives slug-vs-URL spellings). Every
  concurrent job draws on the one shared Claude subscription: keep the
  pool small (2–3), and expect contended jobs to run slower rather than
  fail.
- `GET /checks?repo=owner/name&ref=SHA-or-branch` reads any reachable
  repository's live CI state (check-runs + legacy commit status)
  through the per-repo credential — a $0 synchronous read, no queue
  slot. Answers the same shape the CLI's `--watch-checks` records:
  `{"state", "ok", "concluded", "failed", "summary", …}`.

## Auditability

Every dispatch request already lands in the bounded `access.jsonl`
journal (see `docs/DISPATCH.md`). Sign-ins and refreshes are requests
like any other (`/auth/callback`, `/auth/refresh`), so authentication
events are retained without logging any token material — `Authorization`
headers and bodies are never written.
