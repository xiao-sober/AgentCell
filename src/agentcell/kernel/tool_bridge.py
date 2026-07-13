"""PydanticAI Tool wrappers that preserve AgentCell's single execution entry point."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from pydantic import ValidationError
from pydantic_ai import ApprovalRequired, RunContext, Tool

from agentcell.errors import ToolApprovalRequiredError, ToolArgumentsError
from agentcell.events import JsonValue
from agentcell.kernel.deps import RunDeps
from agentcell.tools import ToolCall, ToolRegistry


def build_agent_tools(
    tool_names: Sequence[str],
    registry: ToolRegistry,
) -> tuple[Tool[RunDeps], ...]:
    """Expose selected schemas while routing every call through ToolExecutor."""

    return tuple(_build_agent_tool(tool_name, registry) for tool_name in tool_names)


def _build_agent_tool(tool_name: str, registry: ToolRegistry) -> Tool[RunDeps]:
    definition = registry.get(tool_name)

    async def invoke(context: RunContext[RunDeps], **arguments: object) -> object:
        call = ToolCall(
            provider_call_id=context.tool_call_id,
            tool_name=definition.name,
            arguments=cast(dict[str, JsonValue], arguments),
        )
        approval_granted = (
            context.tool_call_approved or definition.name in context.deps.temporary_approved_tools
        )
        try:
            result = await context.deps.tools.execute(
                call,
                context.deps.tool_context(),
                approval_granted=approval_granted,
            )
        except ToolApprovalRequiredError as error:
            preview = None
            if definition.approval_previewer is not None:
                try:
                    params = definition.params_model.model_validate(arguments)
                except ValidationError as validation_error:
                    raise ToolArgumentsError(definition.name) from validation_error
                preview = await definition.approval_previewer(
                    params,
                    context.deps.tool_context(),
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

    return Tool.from_schema(
        invoke,
        name=definition.name,
        description=definition.description,
        json_schema=definition.params_model.model_json_schema(),
        takes_ctx=True,
        sequential=True,
    )
