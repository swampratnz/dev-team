"""Tests for the exception hierarchy."""

from __future__ import annotations

import pytest

from dev_team.errors import (
    AgentResponseError,
    DependencyCycleError,
    DevTeamError,
    JSONExtractionError,
    WorkflowError,
)


def test_json_extraction_error_short_text():
    err = JSONExtractionError("nope")
    assert err.text == "nope"
    assert "nope" in str(err)
    assert isinstance(err, DevTeamError)


def test_json_extraction_error_truncates_long_text():
    text = "x" * 500
    err = JSONExtractionError(text)
    assert err.text == text
    assert "..." in str(err)


def test_agent_response_error_short_and_long():
    short = AgentResponseError("engineer", "bad")
    assert short.role == "engineer"
    assert "engineer" in str(short)

    long = AgentResponseError("qa", "y" * 400)
    assert "..." in str(long)
    assert long.role == "qa"


def test_dependency_cycle_error():
    err = DependencyCycleError(["A", "B"])
    assert err.task_ids == ["A", "B"]
    assert "A, B" in str(err)


def test_workflow_error_is_dev_team_error():
    assert issubclass(WorkflowError, DevTeamError)
    with pytest.raises(DevTeamError):
        raise WorkflowError("boom")
