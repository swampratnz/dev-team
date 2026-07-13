# BPG Engineering Standards for AI-Assisted Development

This repo builds and operates AI agents whose output reaches Black Pearl Group products, systems, and repositories. These standards therefore apply in full. They encode Black Pearl Group's Secure Development Policy, Information Security Policy (AUP), AI Agent IAM Policy, and AI Use and Governance Policy into working rules for building tooling with Claude Code.

**Precedence:** These rules win over convenience, speed, or a cleaner diff. Where a rule and a request conflict, follow the rule and say so. Where a situation is not covered or is ambiguous, stop and ask rather than guess. Scope is set by the destination of the code, not by who wrote it or what it was originally for: if the output reaches a BPG product, system, or infrastructure, or processes customer, personal, or confidential data, these standards apply in full.

## 0. Classify before you build

Before writing code, establish two things and state them back:
1. **Data classification** of everything the code and the session will touch. Confidential covers source code, customer data, personal data, credentials and secrets, financial, salary and strategic information, and vulnerability or litigation material. Restricted is the default for internal information. Public is everything cleared for release.
2. **Tier.** Lightweight if internal, non production, non confidential. Full if destined for production, or it processes customer, personal, or confidential data, or it touches production infrastructure. Full tier applies every gate below.

Never enter Confidential or Restricted data into an AI tool or account that is not approved for it in the BPG Application Catalogue. A personally owned or consumer account may receive Public data only, and must never ingest a BPG repository, customer or personal data, or secrets.

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

If the tool acts autonomously against BPG systems, it is an agent and the AI Agent IAM Policy applies. No agent holds credentials in its runtime context. Authenticate only through an approved pattern: Workload Identity Federation via Entra (Pattern A), IdP-delegated OAuth (Pattern B), or Credential Proxy Injection from Key Vault (Pattern C). Agents use their own distinct identity, never a human's credentials, delegated token, or personal OAuth session. Register every agent in the Agent Registry before it calls production or staging, with a named business owner, its permission scope and justification, and its authorised tool servers. Connect only to tool servers on the VP Security allowlist. Tool servers reaching finance, HR, or customer data need VP Security sign-off. Every agent authentication and call must produce a retained, reviewable log; a log gap is a control failure.

## 8. Environments, testing, and release

Keep production, test or staging, and development logically or physically separate. Do not use customer data for testing without the business data owner's and the VP of Development or VP of Platform's permission. No code reaches production without documented, successful security test results and evidence that findings were remediated. Scan application code before deployment and remediate materially security-relevant vulnerabilities within 90 days. Complete the Release Checklist, including all test plans, before deploying.

## 9. When to stop and escalate

Stop and raise it rather than proceed if any of these are true: the work would put Confidential or Restricted data into a non-approved or personal account or tool; a required credential is not available through Key Vault or an approved pattern; an agent would need a scope or a tool server that is not approved; you cannot produce audit logs for what the code does; or the request conflicts with any rule above. Route tool or access requests to ISD, and security or privacy concerns to security@blackpearl.com. Exceptions to the Secure Development Policy go to the Head of Infrastructure and Security with CTO approval.

## Definition of done

Data classification and tier stated. No secrets in code, config, logs, or context. Least privilege applied. Dependencies verified. Security-sensitive code reviewed. Prompt-injection and untrusted-output handling in place for any AI feature. Agents registered with an approved auth pattern and allowlisted tool servers. Tests passed and evidenced. Release Checklist complete. A named owner recorded.
