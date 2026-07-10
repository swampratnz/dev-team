"""Render a :class:`ProjectResult` as text or a JSON-serialisable dict."""

from __future__ import annotations

from typing import Any, Dict

from .models import ProjectResult, TaskResult


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
