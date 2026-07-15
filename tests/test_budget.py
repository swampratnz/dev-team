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


def test_budget_rejects_negative_reserve():
    with pytest.raises(ValueError):
        Budget(reserved_usd=-1.0)


def test_set_reserve_lowers_the_effective_ceiling():
    budget = Budget(limit_usd=10.0)
    budget.set_reserve(3.0)  # effective ceiling is now 7.0
    budget.record("x", _result(6.9))
    assert budget.exhausted is False
    assert budget.remaining == pytest.approx(0.1)
    # crossing the reserved ceiling raises, still under the absolute limit
    with pytest.raises(BudgetExceededError) as excinfo:
        budget.record("x", _result(0.2))  # 7.1 > 7.0 effective, < 10.0 limit
    assert excinfo.value.limit == pytest.approx(7.0)
    assert budget.spent == pytest.approx(7.1)


def test_release_reserve_restores_the_full_ceiling():
    budget = Budget(limit_usd=10.0)
    budget.set_reserve(3.0)
    budget.record("x", _result(7.0))
    assert budget.exhausted is True  # at the reserved ceiling
    budget.set_reserve(0.0)  # release
    assert budget.exhausted is False  # 7.0 < 10.0
    assert budget.remaining == pytest.approx(3.0)


def test_set_reserve_clamps_to_limit_and_floor():
    budget = Budget(limit_usd=5.0)
    budget.set_reserve(100.0)  # clamped to the limit
    assert budget.reserved_usd == 5.0
    assert budget.remaining == 0.0
    assert budget.exhausted is True  # effective ceiling is 0
    budget.set_reserve(-2.0)  # floored at 0
    assert budget.reserved_usd == 0.0
    assert budget.exhausted is False


def test_set_reserve_is_a_noop_when_uncapped():
    budget = Budget()
    budget.set_reserve(3.0)
    budget.record("x", _result(1000.0))
    assert budget.remaining == float("inf")
    assert budget.exhausted is False
