"""Tests for cost accounting and budget enforcement."""

from __future__ import annotations

import pytest

from dev_team.budget import Budget, BudgetExceededError, UsageMeter
from dev_team.sdk import AgentResult


def _result(cost, turns=1):
    return AgentResult(text="", cost_usd=cost, num_turns=turns)


def test_usage_meter_accumulates():
    meter = UsageMeter()
    meter.record("engineer", _result(0.5, 2))
    meter.record("engineer", _result(0.25, 1))
    meter.record("reviewer", _result(0.1, 1))
    assert meter.total_cost == pytest.approx(0.85)
    assert meter.total_turns == 4
    assert meter.call_count == 3
    by_role = meter.cost_by_role()
    assert by_role["engineer"] == pytest.approx(0.75)
    assert by_role["reviewer"] == pytest.approx(0.1)


def test_usage_meter_clamps_negative():
    meter = UsageMeter()
    entry = meter.record("x", _result(-5.0, -3))
    assert entry.cost_usd == 0.0
    assert entry.turns == 0


def test_budget_uncapped_remaining_infinite():
    budget = Budget()
    budget.record("x", _result(1000.0))
    assert budget.remaining == float("inf")
    assert budget.spent == 1000.0


def test_budget_within_limit():
    budget = Budget(limit_usd=1.0)
    budget.record("x", _result(0.4))
    assert budget.remaining == pytest.approx(0.6)


def test_budget_exceeded_raises():
    budget = Budget(limit_usd=1.0)
    budget.record("x", _result(0.6))
    with pytest.raises(BudgetExceededError) as excinfo:
        budget.record("x", _result(0.6))
    assert excinfo.value.limit == 1.0
    assert excinfo.value.spent == pytest.approx(1.2)
    assert budget.remaining == 0.0


def test_budget_rejects_negative_limit():
    with pytest.raises(ValueError):
        Budget(limit_usd=-1.0)
