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

    A releasable *reserve* (:meth:`set_reserve`) temporarily lowers the
    effective ceiling below ``limit_usd`` so an earlier phase (task work) stops
    early and leaves headroom for a later one (the security review that gates
    the commit); releasing it (``set_reserve(0)``) restores the full ceiling.
    The absolute ``limit_usd`` is never exceeded in either phase.
    """

    limit_usd: Optional[float] = None
    meter: UsageMeter = field(default_factory=UsageMeter)
    reserved_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.limit_usd is not None and self.limit_usd < 0:
            raise ValueError("limit_usd must be non-negative")
        if self.reserved_usd < 0:
            raise ValueError("reserved_usd must be non-negative")

    @property
    def _effective_limit(self) -> Optional[float]:
        """The ceiling enforced right now: ``limit_usd`` minus any reserve."""

        if self.limit_usd is None:
            return None
        return max(0.0, self.limit_usd - self.reserved_usd)

    @property
    def spent(self) -> float:
        """Amount spent so far."""

        return self.meter.total_cost

    @property
    def remaining(self) -> float:
        """Remaining budget under the effective ceiling, or infinity when uncapped."""

        limit = self._effective_limit
        if limit is None:
            return float("inf")
        return max(0.0, limit - self.spent)

    @property
    def exhausted(self) -> bool:
        """Whether spending has reached or passed the effective ceiling."""

        limit = self._effective_limit
        return limit is not None and self.spent >= limit

    def set_reserve(self, amount: float) -> None:
        """Hold back ``amount`` of the ceiling for a later phase (0 releases it).

        Clamped to ``[0, limit_usd]``. Only meaningful for a capped budget; on
        an uncapped one the effective ceiling stays infinite regardless.
        """

        amount = max(0.0, amount)
        if self.limit_usd is not None:
            amount = min(amount, self.limit_usd)
        self.reserved_usd = amount

    def check(self) -> None:
        """Raise if the budget is already exhausted (pre-flight guard).

        Raises:
            BudgetExceededError: If spend has already reached the effective
                ceiling (``limit_usd`` minus any active reserve).
        """

        if self.exhausted:
            raise BudgetExceededError(self.spent, self._effective_limit or 0.0)

    def record(self, role: str, result: AgentResult) -> UsageRecord:
        """Record usage, then enforce the effective ceiling.

        Raises:
            BudgetExceededError: If the recorded usage pushed spend over the
                effective ceiling (``limit_usd`` minus any active reserve).
        """

        entry = self.meter.record(role, result)
        limit = self._effective_limit
        if limit is not None and self.spent > limit:
            raise BudgetExceededError(self.spent, limit)
        return entry
