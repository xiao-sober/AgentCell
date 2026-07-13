"""Approval decisions are explicit, strict, and never globally permanent."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentcell.budgets import Budget, BudgetTracker
from agentcell.policy import (
    Approval,
    ApprovalDecision,
    ApprovalDecisionKind,
    RiskLevel,
)


def test_modified_decision_requires_arguments() -> None:
    with pytest.raises(ValidationError, match="modified approval requires arguments"):
        ApprovalDecision(kind=ApprovalDecisionKind.MODIFY)


def test_only_modified_decision_accepts_arguments() -> None:
    with pytest.raises(ValidationError, match="only valid for modified approval"):
        ApprovalDecision(kind=ApprovalDecisionKind.APPROVE, arguments={"value": 1})


def test_rejection_cannot_grant_same_tool_scope() -> None:
    with pytest.raises(ValidationError, match="rejection cannot grant"):
        ApprovalDecision(
            kind=ApprovalDecisionKind.REJECT,
            grant_same_tool=True,
        )


def test_approval_redacts_sensitive_arguments_before_persistence() -> None:
    budget = Budget(
        max_requests=1,
        max_input_tokens=1,
        max_output_tokens=1,
        max_total_tokens=2,
        max_tool_calls=1,
        max_duration_seconds=1,
        max_cost=None,
        max_children=0,
        max_depth=0,
    )
    approval = Approval(
        run_id=uuid4(),
        provider_call_id="call-1",
        agent_id="coordinator",
        agent_name="Coordinator",
        provider="fake",
        model="fake",
        tool_name="test.action",
        arguments={"password": "never-store", "value": 1},
        risk=RiskLevel.GUARDED,
        impact="Test",
        remaining_budget=BudgetTracker(budget).snapshot(),
        idempotent=False,
        timeout_seconds=1,
    )

    assert approval.arguments == {"password": "[REDACTED]", "value": 1}
