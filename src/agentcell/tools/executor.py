"""Policy-aware, budgeted, event-first asynchronous tool execution."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from typing import cast
from uuid import UUID

from pydantic import BaseModel, TypeAdapter, ValidationError

from agentcell.errors import (
    AgentCellError,
    ToolApprovalRequiredError,
    ToolArgumentsError,
    ToolCallDeferredError,
    ToolExecutionError,
    ToolOutputTooLargeError,
    ToolTimeoutError,
)
from agentcell.events import ErrorPayload, EventType, GenericEventPayload, JsonValue
from agentcell.policy import PolicyEngine
from agentcell.tools.models import (
    ToolApprovalPreview,
    ToolCall,
    ToolExecutionContext,
    ToolHandlerOutput,
    ToolResult,
)
from agentcell.tools.registry import ToolRegistry

_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ToolExecutor:
    """Execute registered tools through one ordered security and accounting path."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: PolicyEngine | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy or PolicyEngine()

    async def preflight(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
    ) -> ToolApprovalPreview | None:
        """Validate schema, hard policy, and scoped arguments without accounting or effects."""

        definition = self._registry.get(call.tool_name)
        try:
            params = definition.params_model.model_validate(call.arguments)
        except ValidationError as error:
            raise ToolArgumentsError(call.tool_name) from error
        self._policy.authorize_capabilities(call.tool_name, definition.policy, context.lease)
        preflight = definition.preflight or definition.approval_previewer
        return None if preflight is None else await preflight(params, context)

    async def execute(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        approval_granted: bool = False,
        approval_source: str | None = None,
    ) -> ToolResult:
        """Validate, authorize, account, run, bound output, and emit ordered events."""

        await context.events.emit(
            EventType.TOOL_PROPOSED,
            GenericEventPayload(
                data={
                    "call_id": str(call.call_id),
                    "provider_call_id": call.provider_call_id,
                    "tool_name": call.tool_name,
                    "arguments": _bounded_event_arguments(call.arguments),
                }
            ),
        )
        execution_claimed = False
        change_id: UUID | None = None
        change_completed = False
        try:
            definition = self._registry.get(call.tool_name)
            preview = await self.preflight(call, context)
            params = definition.params_model.model_validate(call.arguments)
            self._policy.require_approval(
                call.tool_name,
                definition.policy,
                approval_granted=approval_granted,
                preview=preview,
            )
            if approval_source is not None:
                if context.approvals is not None:
                    await context.approvals.record(
                        call,
                        context,
                        policy=definition.policy,
                        preview=preview,
                        source=approval_source,
                    )
                else:
                    await context.events.emit(
                        EventType.TOOL_APPROVED,
                        GenericEventPayload(
                            data={
                                "provider_call_id": call.provider_call_id,
                                "tool_name": call.tool_name,
                                "decision_source": approval_source,
                            }
                        ),
                    )
            if context.ledger is not None:
                previous = await context.ledger.begin(
                    call,
                    idempotent=definition.policy.idempotent,
                )
                if previous is not None:
                    await context.events.emit(
                        EventType.TOOL_COMPLETED,
                        GenericEventPayload(
                            data={
                                "call_id": str(call.call_id),
                                "provider_call_id": call.provider_call_id,
                                "tool_name": call.tool_name,
                                "replayed": True,
                                "output_bytes": previous.output_bytes,
                                "output": _bounded_event_output(previous),
                            }
                        ),
                    )
                    return previous
                execution_claimed = call.provider_call_id is not None
            context.budget.reserve_tool_call()
            snapshot = cast(
                dict[str, JsonValue],
                context.budget.snapshot().model_dump(mode="json"),
            )
            await context.events.emit(
                EventType.BUDGET_UPDATED,
                GenericEventPayload(
                    data={
                        "source": "tool",
                        "call_id": str(call.call_id),
                        "tool_name": call.tool_name,
                        "snapshot": snapshot,
                    }
                ),
            )
            await context.events.emit(
                EventType.TOOL_STARTED,
                GenericEventPayload(
                    data={
                        "call_id": str(call.call_id),
                        "provider_call_id": call.provider_call_id,
                        "tool_name": call.tool_name,
                        "timeout_seconds": definition.policy.timeout_seconds,
                    }
                ),
            )

            if context.changes is not None:
                change_id = await context.changes.prepare(call, params, context)

            started_at = monotonic()
            try:
                async with asyncio.timeout(definition.policy.timeout_seconds):
                    raw_output = await definition.handler(params, context)
            except TimeoutError as error:
                raise ToolTimeoutError(
                    call.tool_name,
                    definition.policy.timeout_seconds,
                ) from error
            duration_ms = max(0.0, (monotonic() - started_at) * 1000)
            if change_id is not None and context.changes is not None:
                await context.changes.complete(change_id, context)
                change_completed = True
            output = _normalize_output(raw_output, tool_name=call.tool_name)
            result = await self._bound_output(
                call,
                output,
                duration_ms=duration_ms,
                max_output_bytes=definition.policy.max_output_bytes,
                context=context,
            )
            if context.ledger is not None:
                await context.ledger.complete(call, result)
            await context.events.emit(
                EventType.TOOL_COMPLETED,
                GenericEventPayload(
                    data={
                        "call_id": str(call.call_id),
                        "provider_call_id": call.provider_call_id,
                        "tool_name": call.tool_name,
                        "output_bytes": result.output_bytes,
                        "output": _bounded_event_output(result),
                        "truncated": result.truncated,
                        "artifact": (
                            None
                            if result.artifact is None
                            else result.artifact.model_dump(mode="json")
                        ),
                        "duration_ms": result.duration_ms,
                    }
                ),
            )
            return result
        except (ToolApprovalRequiredError, ToolCallDeferredError):
            raise
        except asyncio.CancelledError:
            if change_id is not None and not change_completed and context.changes is not None:
                await asyncio.shield(context.changes.fail(change_id, context))
            if execution_claimed and context.ledger is not None:
                await context.ledger.fail(call)
            await self._emit_failure(
                call,
                context,
                code="tool_cancelled",
                message="Tool execution was cancelled",
                retryable=False,
            )
            raise
        except AgentCellError as error:
            if change_id is not None and not change_completed and context.changes is not None:
                await context.changes.fail(change_id, context)
            if execution_claimed and context.ledger is not None:
                await context.ledger.fail(call)
            await self._emit_failure(
                call,
                context,
                code=error.code,
                message=str(error),
                retryable=error.retryable,
            )
            raise
        except Exception as error:
            if change_id is not None and not change_completed and context.changes is not None:
                await context.changes.fail(change_id, context)
            if execution_claimed and context.ledger is not None:
                await context.ledger.fail(call)
            classified = ToolExecutionError(call.tool_name)
            await self._emit_failure(
                call,
                context,
                code=classified.code,
                message=str(classified),
                retryable=False,
            )
            raise classified from error

    async def _bound_output(
        self,
        call: ToolCall,
        output: JsonValue,
        *,
        duration_ms: float,
        max_output_bytes: int,
        context: ToolExecutionContext,
    ) -> ToolResult:
        encoded = json.dumps(
            output,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= max_output_bytes:
            return ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                output=output,
                output_bytes=len(encoded),
                duration_ms=duration_ms,
            )
        if context.artifacts is None:
            raise ToolOutputTooLargeError(call.tool_name, len(encoded), max_output_bytes)
        artifact = await context.artifacts.save(
            encoded,
            media_type="application/json",
            suggested_name=f"{call.tool_name}-{call.call_id}.json",
        )
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            output=None,
            output_bytes=len(encoded),
            truncated=True,
            artifact=artifact,
            duration_ms=duration_ms,
        )

    @staticmethod
    async def _emit_failure(
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        await context.events.emit(
            EventType.TOOL_FAILED,
            ErrorPayload(
                code=code,
                message=message,
                retryable=retryable,
                details={
                    "call_id": str(call.call_id),
                    "provider_call_id": call.provider_call_id,
                    "tool_name": call.tool_name,
                },
            ),
        )


def _normalize_output(output: ToolHandlerOutput, *, tool_name: str) -> JsonValue:
    value: object = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    try:
        return _JSON_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise ToolExecutionError(tool_name) from error


def _bounded_event_arguments(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= 32 * 1024:
        return arguments
    return {
        "summary": "Tool arguments omitted from the event payload because they exceed 32 KiB",
        "argument_bytes": len(encoded),
    }


def _bounded_event_output(result: ToolResult) -> JsonValue:
    encoded = json.dumps(
        result.output,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= 32 * 1024:
        return result.output
    return {
        "summary": "Tool output omitted from the event payload because it exceeds 32 KiB",
        "output_bytes": result.output_bytes,
    }
