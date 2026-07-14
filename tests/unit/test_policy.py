"""Capability lease subset and default-deny policy tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentcell.errors import (
    CapabilityDeniedError,
    CapabilityEscalationError,
    ToolApprovalRequiredError,
    ToolForbiddenError,
)
from agentcell.policy import (
    Capability,
    CapabilityLease,
    PermissionMode,
    PolicyEngine,
    RiskLevel,
    ToolPolicy,
)


def test_permission_modes_never_approve_forbidden_operations() -> None:
    assert not PermissionMode.REQUEST.automatically_approves(RiskLevel.GUARDED)
    assert PermissionMode.AUTO.automatically_approves(RiskLevel.GUARDED)
    assert not PermissionMode.AUTO.automatically_approves(RiskLevel.DANGEROUS)
    assert PermissionMode.FULL.automatically_approves(RiskLevel.DANGEROUS)
    assert not PermissionMode.FULL.automatically_approves(RiskLevel.FORBIDDEN)


def test_capability_lease_defaults_to_no_authority() -> None:
    lease = CapabilityLease()

    assert not any(lease.allows(capability) for capability in Capability)


def test_capability_lease_normalizes_scopes_domains_and_commands() -> None:
    lease = CapabilityLease.model_validate(
        {
            "filesystem_read": ["src\\agentcell", "src/agentcell"],
            "filesystem_write": ["docs"],
            "network_domains": ["API.Example.COM."],
            "commands": ["PYTHON", "python"],
            "can_delegate": True,
            "max_child_depth": 2,
        }
    )

    assert lease.filesystem_read == ("src/agentcell",)
    assert lease.network_domains == ("api.example.com",)
    assert lease.commands == frozenset({"python"})
    assert all(lease.allows(capability) for capability in Capability)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("filesystem_read", ["../outside"]),
        ("filesystem_read", ["C:/outside"]),
        ("network_domains", ["http://example.com"]),
        ("network_domains", ["169.254.169.254"]),
        ("network_domains", ["localhost"]),
        ("commands", ["python -c pass"]),
    ],
)
def test_lease_rejects_unsafe_scope_syntax(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        CapabilityLease.model_validate({field: value})


def test_child_lease_can_only_narrow_parent_authority() -> None:
    parent = CapabilityLease(
        filesystem_read=("src",),
        filesystem_write=("src/generated",),
        network_domains=("example.com",),
        commands=frozenset({"python", "pytest"}),
        can_delegate=True,
        max_child_depth=2,
    )
    child = CapabilityLease(
        filesystem_read=("src/agentcell",),
        filesystem_write=("src/generated/client",),
        network_domains=("api.example.com",),
        commands=frozenset({"pytest"}),
        can_delegate=True,
        max_child_depth=1,
    )

    parent.ensure_child_subset(child)


@pytest.mark.parametrize(
    ("child", "capability"),
    [
        (CapabilityLease(filesystem_read=("docs",)), Capability.FILESYSTEM_READ),
        (CapabilityLease(filesystem_write=("src",)), Capability.FILESYSTEM_WRITE),
        (CapabilityLease(network_domains=("openai.com",)), Capability.NETWORK_REQUEST),
        (CapabilityLease(commands=frozenset({"git"})), Capability.SHELL_EXECUTE),
        (
            CapabilityLease(can_delegate=True, max_child_depth=2),
            Capability.AGENT_DELEGATE,
        ),
    ],
)
def test_child_lease_escalation_is_rejected(
    child: CapabilityLease,
    capability: Capability,
) -> None:
    parent = CapabilityLease(
        filesystem_read=("src",),
        filesystem_write=("docs",),
        network_domains=("example.com",),
        commands=frozenset({"python"}),
        can_delegate=True,
        max_child_depth=2,
    )

    with pytest.raises(CapabilityEscalationError) as captured:
        parent.ensure_child_subset(child)

    assert captured.value.capability == capability


def test_dangerous_tool_policy_requires_approval_by_schema() -> None:
    with pytest.raises(ValidationError, match="dangerous tools must require approval"):
        ToolPolicy(
            risk=RiskLevel.DANGEROUS,
            requires_approval=False,
            idempotent=False,
            timeout_seconds=1,
            max_output_bytes=100,
            capabilities=frozenset({Capability.SHELL_EXECUTE}),
        )


def test_policy_engine_checks_capability_approval_and_forbidden_risk() -> None:
    engine = PolicyEngine()
    guarded = ToolPolicy(
        risk=RiskLevel.GUARDED,
        requires_approval=True,
        idempotent=False,
        timeout_seconds=1,
        max_output_bytes=100,
        capabilities=frozenset({Capability.FILESYSTEM_WRITE}),
    )
    write_lease = CapabilityLease(filesystem_write=("docs",))

    with pytest.raises(CapabilityDeniedError):
        engine.authorize_tool("workspace.write", guarded, CapabilityLease())
    with pytest.raises(ToolApprovalRequiredError):
        engine.authorize_tool("workspace.write", guarded, write_lease)
    engine.authorize_tool(
        "workspace.write",
        guarded,
        write_lease,
        approval_granted=True,
    )

    forbidden = guarded.model_copy(update={"risk": RiskLevel.FORBIDDEN})
    with pytest.raises(ToolForbiddenError):
        engine.authorize_tool(
            "workspace.write",
            forbidden,
            write_lease,
            approval_granted=True,
        )
