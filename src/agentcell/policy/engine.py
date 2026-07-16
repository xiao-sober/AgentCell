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
        self.authorize_capabilities(tool_name, policy, lease)
        self.require_approval(tool_name, policy, approval_granted=approval_granted)

    @staticmethod
    def authorize_capabilities(
        tool_name: str,
        policy: ToolPolicy,
        lease: CapabilityLease,
    ) -> None:
        """Enforce unconditional and coarse capability boundaries before preflight."""

        if policy.risk is RiskLevel.FORBIDDEN:
            raise ToolForbiddenError(tool_name)
        for capability in policy.capabilities:
            if not lease.allows(capability):
                raise CapabilityDeniedError(capability)

    @staticmethod
    def require_approval(
        tool_name: str,
        policy: ToolPolicy,
        *,
        approval_granted: bool,
        preview: object | None = None,
    ) -> None:
        """Apply the approval gate only after side-effect-free argument preflight."""

        if policy.requires_approval and not approval_granted:
            raise ToolApprovalRequiredError(tool_name, preview=preview)
