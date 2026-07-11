"""Token/cost accounting and budget enforcement.

Real agent systems must track spend and stop before blowing a budget. The
:class:`UsageMeter` accumulates the cost/turns reported by the Agent SDK, and
:class:`Budget` turns that into an enforceable ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .errors import DevTeamError
from .sdk import AgentResult


class BudgetExceededError(DevTeamError):
    """Raised when a run would exceed its configured cost ceiling."""

    def __init__(self, spent: float, limit: float) -> None:
        super().__init__(
            f"budget exceeded: spent ${spent:.4f} of ${limit:.4f} limit"
        )
        self.spent = spent
        self.limit = limit


@dataclass
class UsageRecord:
    """A single metered agent call."""

    role: str
    cost_usd: float
    turns: int


@dataclass
class UsageMeter:
    """Accumulates usage across agent calls."""

    records: List[UsageRecord] = field(default_factory=list)

    def record(self, role: str, result: AgentResult) -> UsageRecord:
        """Record the usage from ``result`` attributed to ``role``."""

        entry = UsageRecord(
            role=role,
            cost_usd=max(0.0, result.cost_usd),
            turns=max(0, result.num_turns),
        )
        self.records.append(entry)
        return entry

    @property
    def total_cost(self) -> float:
        """Total cost across all recorded calls."""

        return sum(r.cost_usd for r in self.records)

    @property
    def total_turns(self) -> int:
        """Total turns across all recorded calls."""

        return sum(r.turns for r in self.records)

    @property
    def call_count(self) -> int:
        """Number of recorded calls."""

        return len(self.records)

    def cost_by_role(self) -> dict:
        """Return a mapping of role to accumulated cost."""

        totals: dict = {}
        for record in self.records:
            totals[record.role] = totals.get(record.role, 0.0) + record.cost_usd
        return totals


@dataclass
class Budget:
    """An optional spending ceiling backed by a :class:`UsageMeter`.

    Enforcement is check-before / record-after: a call is refused once the
    ceiling is reached, but a call already in flight completes and its full
    cost is recorded. With concurrent agents the overshoot bound is therefore
    the cost of every in-flight call, not zero — treat ``limit_usd`` as a
    stop-line the run halts at gracefully, not a hard cap that can interrupt
    an agent mid-call.
    """

    limit_usd: Optional[float] = None
    meter: UsageMeter = field(default_factory=UsageMeter)

    def __post_init__(self) -> None:
        if self.limit_usd is not None and self.limit_usd < 0:
            raise ValueError("limit_usd must be non-negative")

    @property
    def spent(self) -> float:
        """Amount spent so far."""

        return self.meter.total_cost

    @property
    def remaining(self) -> float:
        """Remaining budget, or infinity when uncapped."""

        if self.limit_usd is None:
            return float("inf")
        return max(0.0, self.limit_usd - self.spent)

    @property
    def exhausted(self) -> bool:
        """Whether spending has reached or passed the ceiling."""

        return self.limit_usd is not None and self.spent >= self.limit_usd

    def check(self) -> None:
        """Raise if the budget is already exhausted (pre-flight guard).

        Raises:
            BudgetExceededError: If spend has already reached the limit.
        """

        if self.exhausted:
            raise BudgetExceededError(self.spent, self.limit_usd or 0.0)

    def record(self, role: str, result: AgentResult) -> UsageRecord:
        """Record usage, then enforce the ceiling.

        Raises:
            BudgetExceededError: If the recorded usage pushed spend over the
                limit.
        """

        entry = self.meter.record(role, result)
        if self.limit_usd is not None and self.spent > self.limit_usd:
            raise BudgetExceededError(self.spent, self.limit_usd)
        return entry
