"""Convert loosely-typed JSON dictionaries into strongly-typed domain models.

Every helper is defensive: language models do not always honour a schema, so
missing keys fall back to sensible defaults and unexpected types are coerced.
"""

from __future__ import annotations

from typing import Any, List, Type, TypeVar

from .models import (
    ChangeType,
    Design,
    DesignComponent,
    DeploymentPlan,
    DocSection,
    Documentation,
    FileChange,
    Implementation,
    Plan,
    ReliabilityReport,
    Review,
    ReviewComment,
    SecurityFinding,
    SecurityReport,
    Severity,
    Task,
    TaskStatus,
    TestCase,
    TestKind,
    TestReport,
)

E = TypeVar("E")


def as_dict(value: Any) -> dict:
    """Return ``value`` if it is a dict, otherwise an empty dict."""

    return value if isinstance(value, dict) else {}


def as_str(data: dict, key: str, default: str = "") -> str:
    """Return ``data[key]`` coerced to a non-empty-friendly string."""

    value = data.get(key)
    if value is None:
        return default
    return str(value)


def as_str_list(data: dict, key: str) -> List[str]:
    """Return ``data[key]`` as a list of strings, coercing as needed."""

    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def as_bool(data: dict, key: str, default: bool = False) -> bool:
    """Return ``data[key]`` interpreted as a boolean."""

    value = data.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "approved", "pass"}
    if value is None:
        return default
    return bool(value)


def as_float(data: dict, key: str, default: float = 0.0) -> float:
    """Return ``data[key]`` interpreted as a float."""

    value = data.get(key)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def as_enum(enum_cls: Type[E], value: Any, default: E) -> E:
    """Return the enum member matching ``value`` (by value), else ``default``."""

    if isinstance(value, str):
        try:
            return enum_cls(value.strip().lower())  # type: ignore[call-arg]
        except ValueError:
            return default
    return default


# Severities models commonly emit that are not literal enum values. Anything
# not recognised at all maps to MAJOR: a gate that cannot understand a
# finding's severity must fail closed (block), never wave it through.
_SEVERITY_SYNONYMS = {
    "blocker": Severity.CRITICAL,
    "high": Severity.MAJOR,
    "medium": Severity.MINOR,
    "low": Severity.INFO,
}


def as_severity(value: Any) -> Severity:
    """Parse a severity value, failing closed on anything unrecognised.

    A missing severity (``None``) stays informational — the model raised no
    alarm — but a severity it *did* set and we cannot interpret blocks.
    """

    if value is None:
        return Severity.INFO
    if isinstance(value, str):
        text = value.strip().lower()
        try:
            return Severity(text)
        except ValueError:
            return _SEVERITY_SYNONYMS.get(text, Severity.MAJOR)
    return Severity.MAJOR


def as_obj_list(data: dict, key: str) -> List[dict]:
    """Return ``data[key]`` as a list of dictionaries."""

    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def task_from_dict(data: dict, index: int) -> Task:
    """Build a :class:`Task` from a JSON dict.

    ``index`` provides a stable fallback identifier when none is supplied.
    """

    data = as_dict(data)
    task_id = as_str(data, "id") or f"T{index + 1}"
    return Task(
        id=task_id,
        title=as_str(data, "title") or task_id,
        description=as_str(data, "description"),
        acceptance_criteria=as_str_list(data, "acceptance_criteria"),
        dependencies=as_str_list(data, "dependencies"),
        status=as_enum(TaskStatus, data.get("status"), TaskStatus.PENDING),
    )


def plan_from_dict(data: Any) -> Plan:
    """Build a :class:`Plan` from a JSON dict."""

    data = as_dict(data)
    tasks = [
        task_from_dict(item, index)
        for index, item in enumerate(as_obj_list(data, "tasks"))
    ]
    return Plan(summary=as_str(data, "summary"), tasks=tasks)


def design_from_dict(data: Any) -> Design:
    """Build a :class:`Design` from a JSON dict."""

    data = as_dict(data)
    components = [
        DesignComponent(
            name=as_str(item, "name"),
            responsibility=as_str(item, "responsibility"),
        )
        for item in as_obj_list(data, "components")
    ]
    return Design(
        overview=as_str(data, "overview"),
        components=components,
        tech_stack=as_str_list(data, "tech_stack"),
        risks=as_str_list(data, "risks"),
        alternatives=as_str_list(data, "alternatives"),
        rationale=as_str(data, "rationale"),
    )


def implementation_from_dict(data: Any, task_id: str) -> Implementation:
    """Build an :class:`Implementation` from a JSON dict."""

    data = as_dict(data)
    files = [
        FileChange(
            path=as_str(item, "path"),
            change_type=as_enum(
                ChangeType, item.get("change_type"), ChangeType.MODIFY
            ),
            summary=as_str(item, "summary"),
            content=as_str(item, "content"),
        )
        for item in as_obj_list(data, "files")
    ]
    return Implementation(
        task_id=task_id,
        summary=as_str(data, "summary"),
        files=files,
        notes=as_str(data, "notes"),
    )


def review_from_dict(data: Any) -> Review:
    """Build a :class:`Review` from a JSON dict."""

    data = as_dict(data)
    comments = [
        ReviewComment(
            severity=as_severity(item.get("severity")),
            message=as_str(item, "message"),
            path=as_str(item, "path") or None,
        )
        for item in as_obj_list(data, "comments")
    ]
    review = Review(
        approved=as_bool(data, "approved"),
        summary=as_str(data, "summary"),
        comments=comments,
    )
    # Enforce the stated review contract: a major or critical comment blocks
    # approval regardless of what the model put in the ``approved`` field.
    if review.approved and review.blocking_comments:
        review.approved = False
    return review


def test_report_from_dict(data: Any) -> TestReport:
    """Build a :class:`TestReport` from a JSON dict."""

    data = as_dict(data)
    cases = [
        TestCase(
            name=as_str(item, "name"),
            kind=as_enum(TestKind, item.get("kind"), TestKind.UNIT),
            target=as_str(item, "target"),
        )
        for item in as_obj_list(data, "cases")
    ]
    return TestReport(
        passed=as_bool(data, "passed"),
        coverage=as_float(data, "coverage"),
        summary=as_str(data, "summary"),
        cases=cases,
    )


def deployment_from_dict(data: Any) -> DeploymentPlan:
    """Build a :class:`DeploymentPlan` from a JSON dict."""

    data = as_dict(data)
    return DeploymentPlan(
        environment=as_str(data, "environment") or "production",
        summary=as_str(data, "summary"),
        steps=as_str_list(data, "steps"),
        rollback=as_str_list(data, "rollback"),
    )


def security_report_from_dict(data: Any) -> SecurityReport:
    """Build a :class:`SecurityReport` from a JSON dict."""

    data = as_dict(data)
    findings = [
        SecurityFinding(
            severity=as_severity(item.get("severity")),
            category=as_str(item, "category") or "general",
            description=as_str(item, "description"),
            remediation=as_str(item, "remediation"),
        )
        for item in as_obj_list(data, "findings")
    ]
    report = SecurityReport(
        approved=as_bool(data, "approved"),
        summary=as_str(data, "summary"),
        findings=findings,
    )
    # A major or critical finding blocks release regardless of the model's
    # self-reported ``approved`` flag.
    if report.approved and report.blocking_findings:
        report.approved = False
    return report


def documentation_from_dict(data: Any) -> Documentation:
    """Build a :class:`Documentation` from a JSON dict."""

    data = as_dict(data)
    sections = [
        DocSection(title=as_str(item, "title"), content=as_str(item, "content"))
        for item in as_obj_list(data, "sections")
    ]
    return Documentation(summary=as_str(data, "summary"), sections=sections)


def reliability_from_dict(data: Any) -> ReliabilityReport:
    """Build a :class:`ReliabilityReport` from a JSON dict."""

    data = as_dict(data)
    return ReliabilityReport(
        production_ready=as_bool(data, "production_ready"),
        summary=as_str(data, "summary"),
        slos=as_str_list(data, "slos"),
        risks=as_str_list(data, "risks"),
        runbook=as_str_list(data, "runbook"),
    )
