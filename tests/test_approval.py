"""Tests for human-in-the-loop approval gates."""

from __future__ import annotations

from dev_team.approval import (
    ApprovalGate,
    ApprovalRequest,
    AutoApprover,
    CallbackApprovalGate,
    DenyAll,
    PolicyApprovalGate,
)


def _req(risk="medium"):
    return ApprovalRequest(action="deploy", detail="ship it", risk=risk)


def test_auto_approver():
    gate = AutoApprover()
    assert isinstance(gate, ApprovalGate)
    assert gate.review(_req()).approved is True


def test_deny_all():
    assert DenyAll().review(_req()).approved is False


def test_policy_gate_blocks_high_risk():
    gate = PolicyApprovalGate()
    assert gate.review(_req("low")).approved is True
    denied = gate.review(_req("high"))
    assert denied.approved is False
    assert "high" in denied.reason


def test_policy_gate_custom_block_risks():
    gate = PolicyApprovalGate(block_risks=("medium", "high"))
    assert gate.review(_req("medium")).approved is False


def test_callback_gate_both_paths():
    approving = CallbackApprovalGate(callback=lambda r: True)
    rejecting = CallbackApprovalGate(callback=lambda r: r.risk != "high")
    assert approving.review(_req()).approved is True
    assert rejecting.review(_req("high")).approved is False
    assert "rejected" in rejecting.review(_req("high")).reason
