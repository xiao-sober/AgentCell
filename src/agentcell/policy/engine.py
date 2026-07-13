"""Default-deny policy evaluation for registered tool execution."""

from __future__ import annotations

from agentcell.errors import (
    CapabilityDeniedError,
    ToolApprovalRequiredError,
    ToolForbiddenError,
)
from agentcell.policy.models import CapabilityLease, RiskLevel, ToolPolicy


class PolicyEngine:
    """Authorize one declared ToolPolicy against a Run capability lease."""

    def authorize_tool(
        self,
        tool_name: str,
        policy: ToolPolicy,
        lease: CapabilityLease,
        *,
        approval_granted: bool = False,
    ) -> None:
        if policy.risk is RiskLevel.FORBIDDEN:
            raise ToolForbiddenError(tool_name)
        for capability in policy.capabilities:
            if not lease.allows(capability):
                raise CapabilityDeniedError(capability)
        if policy.requires_approval and not approval_granted:
            raise ToolApprovalRequiredError(tool_name)
