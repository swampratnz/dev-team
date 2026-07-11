"""Tests for defensive JSON-to-model parsing."""

from __future__ import annotations

from dev_team import parsing
from dev_team.models import (
    ChangeType,
    Severity,
    TaskStatus,
    TestKind,
)


# --- primitive coercers -------------------------------------------------


def test_as_dict():
    assert parsing.as_dict({"a": 1}) == {"a": 1}
    assert parsing.as_dict("nope") == {}


def test_as_str():
    assert parsing.as_str({"k": "v"}, "k") == "v"
    assert parsing.as_str({}, "k", default="d") == "d"
    assert parsing.as_str({"k": 5}, "k") == "5"
    assert parsing.as_str({"k": None}, "k", default="d") == "d"


def test_as_str_list():
    assert parsing.as_str_list({"k": ["a", 1, None]}, "k") == ["a", "1"]
    assert parsing.as_str_list({"k": "not a list"}, "k") == []


def test_as_bool():
    assert parsing.as_bool({"k": True}, "k") is True
    assert parsing.as_bool({"k": "yes"}, "k") is True
    assert parsing.as_bool({"k": "no"}, "k") is False
    assert parsing.as_bool({}, "k", default=True) is True
    assert parsing.as_bool({"k": 1}, "k") is True
    assert parsing.as_bool({"k": 0}, "k") is False


def test_as_float():
    assert parsing.as_float({"k": "3.5"}, "k") == 3.5
    assert parsing.as_float({"k": 4}, "k") == 4.0
    assert parsing.as_float({"k": "nan-ish"}, "k", default=1.0) == 1.0
    assert parsing.as_float({}, "k", default=2.0) == 2.0


def test_as_enum():
    assert parsing.as_enum(TaskStatus, "done", TaskStatus.PENDING) is TaskStatus.DONE
    assert parsing.as_enum(TaskStatus, "DONE", TaskStatus.PENDING) is TaskStatus.DONE
    assert (
        parsing.as_enum(TaskStatus, "bogus", TaskStatus.PENDING) is TaskStatus.PENDING
    )
    assert parsing.as_enum(TaskStatus, 123, TaskStatus.PENDING) is TaskStatus.PENDING


def test_as_obj_list():
    assert parsing.as_obj_list({"k": [{"a": 1}, "x", {"b": 2}]}, "k") == [
        {"a": 1},
        {"b": 2},
    ]
    assert parsing.as_obj_list({"k": None}, "k") == []


# --- model builders -----------------------------------------------------


def test_task_from_dict_full():
    task = parsing.task_from_dict(
        {
            "id": "X1",
            "title": "Title",
            "description": "desc",
            "acceptance_criteria": ["a"],
            "dependencies": ["X0"],
            "status": "in_progress",
        },
        0,
    )
    assert task.id == "X1"
    assert task.title == "Title"
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.dependencies == ["X0"]


def test_task_from_dict_defaults():
    task = parsing.task_from_dict({}, 2)
    assert task.id == "T3"
    assert task.title == "T3"
    assert task.status is TaskStatus.PENDING


def test_plan_from_dict():
    plan = parsing.plan_from_dict(
        {"summary": "s", "tasks": [{"id": "A"}, "ignored", {"id": "B"}]}
    )
    assert plan.summary == "s"
    assert [t.id for t in plan.tasks] == ["A", "B"]


def test_plan_from_dict_non_dict():
    plan = parsing.plan_from_dict("nonsense")
    assert plan.summary == ""
    assert plan.tasks == []


def test_design_from_dict():
    design = parsing.design_from_dict(
        {
            "overview": "o",
            "components": [{"name": "C", "responsibility": "r"}],
            "tech_stack": ["py"],
            "risks": ["r1"],
        }
    )
    assert design.overview == "o"
    assert design.components[0].name == "C"
    assert design.tech_stack == ["py"]


def test_implementation_from_dict():
    impl = parsing.implementation_from_dict(
        {
            "summary": "s",
            "files": [
                {
                    "path": "a.py",
                    "change_type": "create",
                    "summary": "adds",
                    "content": "x",
                }
            ],
            "notes": "n",
        },
        "T1",
    )
    assert impl.task_id == "T1"
    assert impl.files[0].change_type is ChangeType.CREATE
    assert impl.files[0].content == "x"


def test_implementation_from_dict_default_change_type():
    impl = parsing.implementation_from_dict(
        {"files": [{"path": "a.py"}]}, "T2"
    )
    assert impl.files[0].change_type is ChangeType.MODIFY


def test_review_from_dict():
    review = parsing.review_from_dict(
        {
            "approved": False,
            "summary": "s",
            "comments": [
                {"severity": "critical", "path": "a.py", "message": "boom"},
                {"message": "no path here"},
            ],
        }
    )
    assert review.approved is False
    assert review.comments[0].severity is Severity.CRITICAL
    assert review.comments[0].path == "a.py"
    assert review.comments[1].severity is Severity.INFO
    assert review.comments[1].path is None


def test_test_report_from_dict():
    report = parsing.test_report_from_dict(
        {
            "passed": True,
            "coverage": 95.5,
            "summary": "s",
            "cases": [{"name": "t", "kind": "integration", "target": "x"}],
        }
    )
    assert report.passed is True
    assert report.coverage == 95.5
    assert report.cases[0].kind is TestKind.INTEGRATION


def test_deployment_from_dict_defaults_environment():
    plan = parsing.deployment_from_dict({"summary": "s"})
    assert plan.environment == "production"
    assert plan.steps == []


def test_deployment_from_dict_full():
    plan = parsing.deployment_from_dict(
        {
            "environment": "staging",
            "summary": "s",
            "steps": ["a"],
            "rollback": ["b"],
        }
    )
    assert plan.environment == "staging"
    assert plan.steps == ["a"]
    assert plan.rollback == ["b"]


def test_security_report_from_dict():
    from dev_team.models import Severity

    report = parsing.security_report_from_dict(
        {
            "approved": False,
            "summary": "bad",
            "findings": [
                {"severity": "major", "category": "authz", "description": "d", "remediation": "fix"},
                {"description": "no category here"},
            ],
        }
    )
    assert report.approved is False
    assert report.findings[0].severity is Severity.MAJOR
    assert report.findings[0].category == "authz"
    assert report.findings[1].category == "general"
    assert report.findings[1].severity is Severity.INFO


def test_documentation_from_dict():
    docs = parsing.documentation_from_dict(
        {"summary": "s", "sections": [{"title": "T", "content": "C"}]}
    )
    assert docs.summary == "s"
    assert docs.sections[0].title == "T"


def test_reliability_from_dict():
    report = parsing.reliability_from_dict(
        {
            "production_ready": True,
            "summary": "s",
            "slos": ["a"],
            "risks": ["b"],
            "runbook": ["c"],
        }
    )
    assert report.production_ready is True
    assert report.slos == ["a"]
    assert report.runbook == ["c"]


def test_review_blocking_comment_forces_rejection():
    data = {
        "approved": True,
        "summary": "lgtm",
        "comments": [{"severity": "critical", "message": "broken"}],
    }
    review = parsing.review_from_dict(data)
    assert review.approved is False


def test_security_blocking_finding_forces_rejection():
    data = {
        "approved": True,
        "summary": "fine",
        "findings": [{"severity": "major", "category": "injection", "description": "sqli"}],
    }
    report = parsing.security_report_from_dict(data)
    assert report.approved is False


def test_design_alternatives_and_rationale():
    data = {
        "overview": "o",
        "alternatives": ["queue-based — too complex for the load"],
        "rationale": "simplest thing that meets the SLO",
    }
    design = parsing.design_from_dict(data)
    assert design.alternatives == ["queue-based — too complex for the load"]
    assert design.rationale == "simplest thing that meets the SLO"
