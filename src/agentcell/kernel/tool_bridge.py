"""PydanticAI Tool wrappers that preserve AgentCell's single execution entry point."""

from __future__ import annotations

from collections.abc import Sequence
from math import ceil
from typing import cast

from pydantic import ValidationError
from pydantic_ai import ApprovalRequired, CallDeferred, RunContext, Tool, ToolDefinition

from agentcell.budgets import BudgetTracker
from agentcell.errors import (
    ToolApprovalRequiredError,
    ToolArgumentsError,
    ToolCallDeferredError,
    ToolRegistrationError,
)
from agentcell.events import JsonValue
from agentcell.kernel.deps import RunDeps
from agentcell.providers.tool_names import portable_tool_name
from agentcell.tools import ToolCall, ToolRegistry

FINAL_OUTPUT_RETRIES = 2
FINAL_REQUEST_ATTEMPTS = FINAL_OUTPUT_RETRIES + 1


def reserve_final_model_request(
    context: RunContext[RunDeps],
    tool_definition: ToolDefinition,
) -> ToolDefinition | None:
    """Hide tools when only the final model request remains so the Run can synthesize."""

    if not context.deps.has_deferred_tool_results and _should_finalize(context.deps.budget):
        return None
    return tool_definition


def budget_instructions(context: RunContext[RunDeps]) -> str:
    """Provide a cache-stable budget policy and a stable final-window instruction."""

    tracker = context.deps.budget
    if _should_finalize(tracker):
        return (
            "The Run has entered its reserved final-answer window. Do not call or propose any "
            "tool, even if earlier messages used tools. Produce the best final answer now from "
            f"evidence already collected. You have at most {FINAL_REQUEST_ATTEMPTS} attempts to "
            "produce a valid final response."
        )
    budget = tracker.budget
    return (
        f"This Run has fixed limits of {budget.max_requests} model requests, "
        f"{budget.max_tool_calls} tool calls, {budget.max_input_tokens} input tokens, and "
        f"{budget.max_total_tokens} total tokens. Avoid exhaustive traversal; the runtime "
        "will reserve a final-answer window, after which tools are unavailable."
    )


def _should_finalize(tracker: BudgetTracker) -> bool:
    remaining = tracker.remaining
    max_requests = tracker.budget.max_requests
    request_reserve = (
        0 if max_requests == 0 else min(FINAL_REQUEST_ATTEMPTS, max(1, max_requests // 5))
    )
    tool_reserve = min(4, tracker.budget.max_tool_calls // 5)
    count_limit_reached = (request_reserve > 0 and remaining.requests <= request_reserve) or (
        tool_reserve > 0 and remaining.tool_calls <= tool_reserve
    )
    if count_limit_reached:
        return True

    usage = tracker.usage
    if usage.requests == 0 or usage.input_tokens == 0:
        return False
    average_input = ceil(usage.input_tokens / usage.requests)
    predicted_input = max(tracker.last_model_input_tokens, average_input)
    # Continuing to expose tools must leave capacity for both the next exploratory
    # request and the final synthesis request. The 10% margin absorbs context growth.
    input_reserve = ceil(predicted_input * 2.2)
    output_reserve = min(
        tracker.budget.max_output_tokens,
        max(256, min(8_192, tracker.budget.max_output_tokens // 5)),
    )
    return (
        remaining.input_tokens <= input_reserve
        or remaining.output_tokens <= output_reserve
        or remaining.total_tokens <= input_reserve + output_reserve
    )


def build_agent_tools(
    tool_names: Sequence[str],
    registry: ToolRegistry,
) -> tuple[Tool[RunDeps], ...]:
    """Expose selected schemas while routing every call through ToolExecutor."""

    tools = tuple(_build_agent_tool(tool_name, registry) for tool_name in tool_names)
    aliases = [tool.name for tool in tools]
    if len(aliases) != len(set(aliases)):
        raise ToolRegistrationError("Provider-facing tool aliases must be unique")
    return tools


def _build_agent_tool(tool_name: str, registry: ToolRegistry) -> Tool[RunDeps]:
    definition = registry.get(tool_name)

    async def invoke(context: RunContext[RunDeps], **arguments: object) -> object:
        call = ToolCall(
            provider_call_id=context.tool_call_id,
            tool_name=definition.name,
            arguments=cast(dict[str, JsonValue], arguments),
        )
        policy_approved = context.deps.permission_mode.automatically_approves(
            definition.policy.risk
        )
        approval_granted = (
            context.tool_call_approved
            or definition.name in context.deps.temporary_approved_tools
            or policy_approved
        )
        try:
            result = await context.deps.tools.execute(
                call,
                context.deps.tool_context(provider_call_id=context.tool_call_id),
                approval_granted=approval_granted,
                approval_source=(
                    f"policy-{context.deps.permission_mode.value}"
                    if policy_approved and not context.tool_call_approved
                    else None
                ),
            )
        except ToolCallDeferredError as error:
            raise CallDeferred(metadata=error.metadata) from error
        except ToolApprovalRequiredError as error:
            preview = None
            if definition.approval_previewer is not None:
                try:
                    params = definition.params_model.model_validate(arguments)
                except ValidationError as validation_error:
                    raise ToolArgumentsError(definition.name) from validation_error
                preview = await definition.approval_previewer(
                    params,
                    context.deps.tool_context(provider_call_id=context.tool_call_id),
                )
            raise ApprovalRequired(
                metadata={
                    "tool_name": definition.name,
                    "arguments": arguments,
                    "risk": definition.policy.risk.value,
                    "impact": (
                        definition.description
                        if preview is None or preview.impact is None
                        else preview.impact
                    ),
                    "diff": None if preview is None else preview.diff,
                    "diff_artifact": (
                        None
                        if preview is None or preview.diff_artifact is None
                        else preview.diff_artifact.model_dump(mode="json")
                    ),
                    "idempotent": definition.policy.idempotent,
                    "timeout_seconds": definition.policy.timeout_seconds,
                    "agent_id": context.deps.agent_id,
                    "agent_name": context.deps.agent_name,
                    "provider": context.deps.provider,
                    "model": context.deps.model,
                }
            ) from error
        return result.model_dump(mode="json")

    tool = cast(
        Tool[RunDeps],
        Tool.from_schema(
            invoke,
            name=portable_tool_name(definition.name),
            description=definition.description,
            json_schema=definition.params_model.model_json_schema(),
            takes_ctx=True,
            sequential=True,
        ),
    )
    tool.prepare = reserve_final_model_request
    return tool
