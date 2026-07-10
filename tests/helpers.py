"""Shared helpers for the dev-team test suite."""

from __future__ import annotations

import asyncio
from typing import Any

from dev_team.testing import json_response


def run(coro: Any) -> Any:
    """Run an async coroutine to completion for synchronous tests."""

    return asyncio.run(coro)


def plan_dict(task_count: int = 1) -> dict:
    tasks = [
        {
            "id": f"T{i + 1}",
            "title": f"Task {i + 1}",
            "description": "do the thing",
            "acceptance_criteria": ["it works"],
            "dependencies": [],
        }
        for i in range(task_count)
    ]
    return {"summary": "the plan", "tasks": tasks}


def design_dict() -> dict:
    return {
        "overview": "the design",
        "components": [{"name": "Core", "responsibility": "does core things"}],
        "tech_stack": ["python"],
        "risks": ["none really"],
    }


def impl_dict() -> dict:
    return {
        "summary": "implemented",
        "files": [
            {
                "path": "src/x.py",
                "change_type": "create",
                "summary": "adds x",
                "content": "x = 1",
            }
        ],
        "notes": "clean",
    }


def review_dict(approved: bool = True) -> dict:
    comments = (
        []
        if approved
        else [{"severity": "major", "path": "src/x.py", "message": "fix this"}]
    )
    return {
        "approved": approved,
        "summary": "looks good" if approved else "needs work",
        "comments": comments,
    }


def qa_report_dict(passed: bool = True, coverage: float = 100.0) -> dict:
    return {
        "passed": passed,
        "coverage": coverage,
        "summary": "tests ran",
        "cases": [{"name": "t1", "kind": "unit", "target": "x"}],
    }


def deploy_dict() -> dict:
    return {
        "environment": "production",
        "summary": "ship it",
        "steps": ["build", "deploy"],
        "rollback": ["revert"],
    }


def happy_responses(task_count: int = 1) -> list:
    """Build an ordered response queue for a fully-successful run."""

    responses = [json_response(plan_dict(task_count)), json_response(design_dict())]
    for _ in range(task_count):
        responses.append(json_response(impl_dict()))
        responses.append(json_response(review_dict(True)))
        responses.append(json_response(qa_report_dict(True)))
    responses.append(json_response(deploy_dict()))
    return responses


def security_dict(approved=True):
    findings = (
        []
        if approved
        else [
            {
                "severity": "critical",
                "category": "injection",
                "description": "sqli",
                "remediation": "parameterize",
            }
        ]
    )
    return {
        "approved": approved,
        "summary": "secure" if approved else "vulnerable",
        "findings": findings,
    }


def docs_dict():
    return {"summary": "docs", "sections": [{"title": "Overview", "content": "..."}]}


def reliability_dict(ready=True):
    return {
        "production_ready": ready,
        "summary": "ready" if ready else "not ready",
        "slos": ["99.9%"],
        "risks": [],
        "runbook": ["restart"],
    }


def engine_responses(*, review=True, security=True, reliability=True):
    """Keyed-by-system-prompt responses covering every engine agent."""

    return {
        "product manager": json_response(plan_dict(1)),
        "software architect": json_response(design_dict()),
        "senior software engineer": json_response(impl_dict()),
        "code reviewer": json_response(review_dict(review)),
        "application security engineer": json_response(security_dict(security)),
        "technical writer": json_response(docs_dict()),
        "site reliability engineer": json_response(reliability_dict(reliability)),
        "DevOps engineer": json_response(deploy_dict()),
    }
