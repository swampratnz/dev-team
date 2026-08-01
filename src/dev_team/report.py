"""Render run results as text or JSON-serialisable dicts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict

from .fences import ZERO_WIDTH_SPACE
from .models import ProjectResult, TaskResult

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from .engine import DeliveryOutcome

# GitHub's recognised closing keywords, immediately followed by an issue
# reference. GitHub auto-closes the referenced issue when a PR body
# containing this adjacency merges to the default branch. Per GitHub's
# "linking a pull request to an issue" docs, the reference may be a bare
# ``#123``/``GH-123``, a cross-repo shorthand (``owner/repo#123``), or the
# issue's full URL (``https://github.com/owner/repo/issues/123``).
_CLOSING_KEYWORD_RE = re.compile(
    r"\b(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"(\s*)"
    r"(#\d+"
    r"|GH-\d+"
    r"|[\w.-]+/[\w.-]+#\d+"
    r"|https?://github\.com/[^/\s]+/[^/\s]+/issues/\d+)",
    re.IGNORECASE,
)
# Raw markdown inline link/image syntax: ``[text](url)`` or ``![alt](url)``.
# Both share the ``](`` adjacency that turns the preceding brackets into a
# live link/image once rendered.
_MARKDOWN_LINK_RE = re.compile(r"\]\(")
# CommonMark reference-style link/image usage: ``[text][label]`` (and the
# collapsed ``[text][]`` form) share the ``][`` adjacency with the preceding
# bracket. Also covers the alternate ``[label]:`` reference-definition
# adjacency (``]:``), so a would-be definition line can't bind a label to a
# URL either.
_MARKDOWN_REF_RE = re.compile(r"\]([:\[])")
# A raw HTML tag opening, e.g. ``<details>`` or ``</summary>``.
_HTML_TAG_RE = re.compile(r"<(/?[A-Za-z])")
# Any line break embedded in a single free-text field. Each sanitized field
# is rendered as (part of) exactly one line in the summary; an embedded
# newline would otherwise let injected text start a new line of its own —
# forging an extra "Security:"/"Reliability:"-style summary line, or letting
# a CommonMark link reference definition (which must be the sole content of
# its line) land on one.
_NEWLINE_RE = re.compile(r"\r\n|\r|\n")


def _sanitize_visual_text(text: str) -> str:
    """Neutralise a visual-review free-text field for the PR-body sink.

    ``text`` is model output derived from reading a screenshot of an
    untrusted, cloned repo's served app, so it is untrusted (see
    ``visualreview._report_from_payload``). ``engine._render_visual_findings``
    already defuses this same text against the ``visual-findings`` prompt
    fence before it re-enters an agent prompt; this covers the other named
    consumer, ``render_delivery_summary``, which renders it as the literal
    GitHub PR body. Unlike a prompt fence, the PR body's live syntax is
    GitHub's closing-keyword parser and CommonMark markup, so a different
    neutralisation target — but the same invisible zero-width-space-break
    idiom ``fences.defuse`` uses, which is inherently idempotent (there is no
    literal adjacency left to re-break) and never alters non-matching text.
    """

    text = _NEWLINE_RE.sub(" ", text)
    text = _CLOSING_KEYWORD_RE.sub(
        lambda m: m.group(1) + ZERO_WIDTH_SPACE + m.group(2) + m.group(3), text
    )
    text = _MARKDOWN_LINK_RE.sub("]" + ZERO_WIDTH_SPACE + "(", text)
    text = _MARKDOWN_REF_RE.sub(lambda m: "]" + ZERO_WIDTH_SPACE + m.group(1), text)
    text = _HTML_TAG_RE.sub(lambda m: "<" + ZERO_WIDTH_SPACE + m.group(1), text)
    return text


def _task_to_dict(result: TaskResult) -> Dict[str, Any]:
    task = result.task
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status.value,
        "attempts": result.attempts,
        "succeeded": result.succeeded,
        "review_approved": result.review.approved if result.review else None,
        "tests_passed": result.test_report.passed if result.test_report else None,
        "coverage": result.test_report.coverage if result.test_report else None,
    }


def result_to_dict(result: ProjectResult) -> Dict[str, Any]:
    """Convert ``result`` into a JSON-serialisable dictionary."""

    deployment = result.deployment
    return {
        "request": {
            "title": result.request.title,
            "description": result.request.description,
            "constraints": list(result.request.constraints),
        },
        "success": result.success,
        # The simulation makes real, paid agent calls; surface what it spent
        # (metered into ProjectResult.cost_usd by the workflow's usage meter).
        "cost_usd": result.cost_usd,
        "plan_summary": result.plan.summary,
        "design_overview": result.design.overview,
        "tech_stack": list(result.design.tech_stack),
        "tasks": [_task_to_dict(tr) for tr in result.task_results],
        "deployment": (
            {
                "environment": deployment.environment,
                "summary": deployment.summary,
                "steps": list(deployment.steps),
                "rollback": list(deployment.rollback),
            }
            if deployment is not None
            else None
        ),
    }


def render_summary(result: ProjectResult) -> str:
    """Render a human-readable summary of ``result``."""

    lines = []
    lines.append(f"Feature: {result.request.title}")
    verdict = "SUCCESS" if result.success else "INCOMPLETE"
    lines.append(f"Result:  {verdict}")
    # The simulation runs real, paid agents — always report what it spent so
    # the "simulation" label never reads as "free" (metered into cost_usd).
    lines.append(f"Cost:    ${result.cost_usd:.4f}")
    lines.append("")
    lines.append(f"Plan: {result.plan.summary}")
    lines.append(f"Design: {result.design.overview}")
    if result.design.tech_stack:
        lines.append(f"Stack: {', '.join(result.design.tech_stack)}")
    lines.append("")
    lines.append("Tasks:")
    if result.task_results:
        for tr in result.task_results:
            mark = "✓" if tr.succeeded else "✗"
            lines.append(
                f"  {mark} {tr.task.id} {tr.task.title} "
                f"[{tr.task.status.value}] ({tr.attempts} attempt(s))"
            )
    else:
        lines.append("  (no tasks were produced)")
    if result.deployment is not None:
        lines.append("")
        lines.append(
            f"Deployment ({result.deployment.environment}): "
            f"{result.deployment.summary}"
        )
    return "\n".join(lines)


def delivery_to_dict(outcome: "DeliveryOutcome") -> Dict[str, Any]:
    """Convert a :class:`~dev_team.engine.DeliveryOutcome` to a dict."""

    return {
        "request": {
            "title": outcome.request.title,
            "description": outcome.request.description,
            "constraints": list(outcome.request.constraints),
        },
        "success": outcome.success,
        "plan_summary": outcome.plan_summary,
        "design_overview": outcome.design.overview,
        "tasks": [_task_to_dict(tr) for tr in outcome.task_results],
        "security_approved": outcome.security.approved if outcome.security else None,
        "security_scanner_failed": (
            outcome.security.scanner_failed if outcome.security else None
        ),
        "production_ready": (
            outcome.reliability.production_ready if outcome.reliability else None
        ),
        "committed": outcome.committed,
        "visual_summary": outcome.visual.summary if outcome.visual else None,
        "visual_findings": (
            [
                {"route": f.route, "issue": f.issue, "severity": f.severity.value}
                for f in outcome.visual.findings
            ]
            if outcome.visual
            else None
        ),
        "pull_request_url": outcome.pull_request_url,
        "pull_request_number": outcome.pull_request_number,
        "checks_state": outcome.checks.state if outcome.checks else None,
        "checks_failed": list(outcome.checks.failed) if outcome.checks else [],
        "budget_exhausted": outcome.budget_exhausted,
        "resumed_task_ids": list(outcome.resumed_task_ids),
        "cost_usd": outcome.cost_usd,
        "workspace_files": list(outcome.workspace_files),
        "branch": outcome.branch,
        "halted_reason": outcome.halted_reason,
        "baseline_green": outcome.baseline.passed if outcome.baseline else None,
        "scorecard": dict(outcome.scorecard),
        "unverified_claims": (
            list(outcome.documentation.unverified_claims) if outcome.documentation else []
        ),
    }


def render_delivery_summary(outcome: "DeliveryOutcome") -> str:
    """Render a human-readable summary of a delivery run."""

    lines = [f"Feature: {outcome.request.title}"]
    verdict = "SUCCESS" if outcome.success else "INCOMPLETE"
    lines.append(f"Result:  {verdict}")
    lines.append(f"Cost:    ${outcome.cost_usd:.4f}")
    if outcome.halted_reason:
        lines.append(f"Halted:  {outcome.halted_reason}")
        if outcome.baseline is not None:
            for gate in outcome.baseline.failed_gates:
                detail = gate.detail[:200]
                if len(gate.detail) > 200:
                    detail += " (full detail in .dev_team/events.jsonl)"
                lines.append(f"  baseline gate failed — {gate.name}: {detail}")
        return "\n".join(lines)
    if outcome.branch:
        lines.append(f"Branch:  {outcome.branch}")
    lines.append("")
    lines.append("Tasks:")
    if outcome.task_results:
        for tr in outcome.task_results:
            mark = "✓" if tr.succeeded else "✗"
            lines.append(
                f"  {mark} {tr.task.id} {tr.task.title} "
                f"[{tr.task.status.value}] ({tr.attempts} attempt(s))"
            )
    else:
        lines.append("  (no tasks were produced)")
    if outcome.security is not None:
        state = "approved" if outcome.security.approved else "BLOCKED"
        marker = " [SCANNER DID NOT RUN]" if outcome.security.scanner_failed else ""
        lines.append(f"Security: {state} — {outcome.security.summary}{marker}")
    if outcome.reliability is not None:
        state = "ready" if outcome.reliability.production_ready else "NOT READY"
        lines.append(f"Reliability: {state}")
    if outcome.visual is not None:
        count = len(outcome.visual.findings)
        state = "clean" if count == 0 else f"{count} finding(s)"
        # outcome.visual.summary and each finding's route/issue are model output
        # derived from reading a screenshot of an untrusted repo's served app
        # (see _sanitize_visual_text's docstring), and this text becomes the
        # literal GitHub PR body — sanitize before it renders as live markup.
        visual_summary = (
            _sanitize_visual_text(outcome.visual.summary) if outcome.visual.summary else ""
        )
        summary = f" — {visual_summary}" if visual_summary else ""
        lines.append(f"Visual (advisory): {state}{summary}")
        for finding in outcome.visual.findings:
            route = _sanitize_visual_text(finding.route)
            issue = _sanitize_visual_text(finding.issue)
            lines.append(f"  [{finding.severity.value}] {route}: {issue}")
    lines.append(f"Committed: {'yes' if outcome.committed else 'no'}")
    if outcome.pull_request_url:
        lines.append(f"Pull request: {outcome.pull_request_url}")
    if outcome.checks is not None:
        line = f"Checks: {outcome.checks.state}"
        if outcome.checks.failed:
            line += f" — {', '.join(outcome.checks.failed)}"
        lines.append(line)
    if outcome.scorecard:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(outcome.scorecard.items()))
        lines.append(f"Scorecard: {counts}")
    if outcome.documentation and outcome.documentation.unverified_claims:
        lines.append(
            f"Unverified doc claims: {len(outcome.documentation.unverified_claims)}"
        )
        lines.extend(f"  {c}" for c in outcome.documentation.unverified_claims)
    if outcome.budget_exhausted:
        lines.append("Budget: EXHAUSTED (run stopped early; resume to continue)")
    if outcome.resumed_task_ids:
        lines.append(f"Resumed from checkpoint: {', '.join(outcome.resumed_task_ids)}")
    if outcome.workspace_files:
        lines.append("")
        lines.append("Files:")
        lines.extend(f"  {path}" for path in outcome.workspace_files)
    return "\n".join(lines)
