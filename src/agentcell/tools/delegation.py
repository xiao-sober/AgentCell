"""Structured Agent-as-Tool registration without Kernel dependencies."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agentcell.agents import DelegationRequest, DelegationResult
from agentcell.errors import ToolCallDeferredError, ToolExecutionError
from agentcell.policy import Capability, RiskLevel, ToolPolicy
from agentcell.tools.models import ToolApprovalPreview, ToolDefinition, ToolExecutionContext
from agentcell.tools.registry import ToolRegistry


class AgentDelegateParams(DelegationRequest):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentDelegateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result: DelegationResult


async def agent_delegate(
    params: AgentDelegateParams,
    context: ToolExecutionContext,
) -> AgentDelegateOutput:
    if context.delegation is None or context.run_id is None:
        raise ToolExecutionError("agent.delegate")
    provider_call_id = context.provider_call_id
    if not isinstance(provider_call_id, str) or not provider_call_id:
        raise ToolExecutionError("agent.delegate")
    result = await context.delegation.delegate(
        DelegationRequest.model_validate(params.model_dump()),
        context,
        provider_call_id=provider_call_id,
    )
    if not result.status.is_terminal:
        raise ToolCallDeferredError(
            {
                "delegation_id": str(result.delegation_id),
                "child_run_id": str(result.child_run_id),
                "agent_id": result.agent_id,
                "approval_ids": [str(value) for value in result.approval_ids],
            }
        )
    return AgentDelegateOutput(result=result)


async def preflight_agent_delegate(
    params: AgentDelegateParams,
    context: ToolExecutionContext,
) -> ToolApprovalPreview:
    if context.delegation is None or context.run_id is None:
        raise ToolExecutionError("agent.delegate")
    provider_call_id = context.provider_call_id
    if not isinstance(provider_call_id, str) or not provider_call_id:
        raise ToolExecutionError("agent.delegate")
    request = DelegationRequest.model_validate(params.model_dump())
    await context.delegation.preflight(
        request,
        context,
        provider_call_id=provider_call_id,
    )
    return ToolApprovalPreview(impact=f"Delegate a bounded task to {request.agent_id}")


def register_delegation_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="agent.delegate",
            description="Delegate one bounded task to a registered child Agent.",
            params_model=AgentDelegateParams,
            policy=ToolPolicy(
                risk=RiskLevel.SAFE,
                requires_approval=False,
                idempotent=True,
                timeout_seconds=3600,
                max_output_bytes=64 * 1024,
                capabilities=frozenset({Capability.AGENT_DELEGATE}),
            ),
            handler=agent_delegate,
            preflight=preflight_agent_delegate,
        )
    )
