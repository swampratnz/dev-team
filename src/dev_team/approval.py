"""Human-in-the-loop approval gates for risky actions.

Production agent systems don't let an agent deploy to production or run a
destructive command unsupervised. An :class:`ApprovalGate` is consulted before
such actions; different implementations encode auto-approval, policy rules, or a
human callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, Sequence, runtime_checkable


@dataclass
class ApprovalRequest:
    """A request to perform a potentially risky action."""

    action: str
    detail: str
    risk: str = "medium"  # low | medium | high


@dataclass
class ApprovalDecision:
    """The outcome of an approval request."""

    approved: bool
    reason: str = ""


@runtime_checkable
class ApprovalGate(Protocol):
    """Decides whether a risky action may proceed."""

    def review(self, request: ApprovalRequest) -> ApprovalDecision:
        """Return an :class:`ApprovalDecision` for ``request``."""
        ...


class AutoApprover:
    """Approves everything — the default for unattended runs."""

    def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=True, reason="auto-approved")


class DenyAll:
    """Denies everything — useful for strictly read-only runs."""

    def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=False, reason="all actions denied by policy")


@dataclass
class PolicyApprovalGate:
    """Approves unless the action's risk is in ``block_risks``."""

    block_risks: Sequence[str] = ("high",)

    def review(self, request: ApprovalRequest) -> ApprovalDecision:
        if request.risk in self.block_risks:
            return ApprovalDecision(
                approved=False,
                reason=f"risk '{request.risk}' requires human approval",
            )
        return ApprovalDecision(approved=True, reason="within policy")


@dataclass
class CallbackApprovalGate:
    """Delegates the decision to a supplied callable (e.g. a human prompt)."""

    callback: Callable[[ApprovalRequest], bool]

    def review(self, request: ApprovalRequest) -> ApprovalDecision:
        approved = self.callback(request)
        return ApprovalDecision(
            approved=approved,
            reason="human approved" if approved else "human rejected",
        )
