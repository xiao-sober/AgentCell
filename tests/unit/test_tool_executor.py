"""ToolRegistry and ordered executor security-boundary tests."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from agentcell.budgets import Budget, BudgetTracker
from agentcell.errors import (
    CapabilityDeniedError,
    ToolArgumentsError,
    ToolExecutionError,
    ToolOutputTooLargeError,
    ToolRegistrationError,
    ToolTimeoutError,
)
from agentcell.events import ArtifactReference, EventPayload, EventType, GenericEventPayload
from agentcell.policy import Capability, CapabilityLease, RiskLevel, ToolPolicy
from agentcell.tools import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolHandler,
    ToolHandlerOutput,
    ToolRegistry,
)


class EchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    password: str | None = None


class NonStrictParams(BaseModel):
    value: str


@dataclass
class RecordingEventSink:
    events: list[tuple[EventType, EventPayload]] = field(default_factory=lambda: [])

    async def emit(self, event_type: EventType, payload: EventPayload) -> None:
        self.events.append((event_type, payload))


@dataclass
class RecordingArtifactStore:
    saved: list[bytes] = field(default_factory=lambda: [])

    async def save(
        self,
        content: bytes,
        *,
        media_type: str,
        suggested_name: str,
    ) -> ArtifactReference:
        del suggested_name
        self.saved.append(content)
        return ArtifactReference(
            artifact_id=uuid4(),
            media_type=media_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )


def _budget(*, max_tool_calls: int = 5) -> BudgetTracker:
    return BudgetTracker(
        Budget(
            max_requests=5,
            max_input_tokens=100,
            max_output_tokens=100,
            max_total_tokens=200,
            max_tool_calls=max_tool_calls,
            max_duration_seconds=30,
            max_cost=Decimal("1"),
            max_children=0,
            max_depth=0,
        )
    )


def _policy(
    *,
    capability: Capability = Capability.FILESYSTEM_READ,
    timeout_seconds: float = 1,
    max_output_bytes: int = 1024,
    idempotent: bool = True,
) -> ToolPolicy:
    return ToolPolicy(
        risk=RiskLevel.SAFE,
        requires_approval=False,
        idempotent=idempotent,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        capabilities=frozenset({capability}),
    )


def _context(
    workspace: Path,
    *,
    lease: CapabilityLease | None = None,
    events: RecordingEventSink | None = None,
    artifacts: RecordingArtifactStore | None = None,
    max_tool_calls: int = 5,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        workspace=workspace,
        lease=lease or CapabilityLease(filesystem_read=(".",)),
        budget=_budget(max_tool_calls=max_tool_calls),
        events=events or RecordingEventSink(),
        artifacts=artifacts,
    )


async def _echo(params: EchoParams, context: ToolExecutionContext) -> ToolHandlerOutput:
    del context
    return {"text": params.text}


def _registry_with(
    handler: ToolHandler[EchoParams] = _echo,
    *,
    policy: ToolPolicy | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="test.echo",
            description="Echo validated text.",
            params_model=EchoParams,
            policy=policy or _policy(),
            handler=handler,
        )
    )
    return registry


def test_registry_rejects_duplicates_and_non_strict_parameters() -> None:
    registry = _registry_with()

    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register(
            ToolDefinition(
                name="test.echo",
                description="Duplicate.",
                params_model=EchoParams,
                policy=_policy(),
                handler=_echo,
            )
        )

    async def non_strict_handler(
        params: NonStrictParams,
        context: ToolExecutionContext,
    ) -> str:
        del context
        return params.value

    with pytest.raises(ToolRegistrationError, match="extra='forbid'"):
        registry.register(
            ToolDefinition(
                name="test.non_strict",
                description="Invalid schema.",
                params_model=NonStrictParams,
                policy=_policy(),
                handler=non_strict_handler,
            )
        )


@pytest.mark.asyncio
async def test_executor_emits_ordered_events_and_reserves_budget(tmp_path: Path) -> None:
    sink = RecordingEventSink()
    context = _context(tmp_path, events=sink)
    executor = ToolExecutor(_registry_with())

    result = await executor.execute(
        ToolCall(tool_name="test.echo", arguments={"text": "hello"}),
        context,
    )

    assert result.output == {"text": "hello"}
    assert context.budget.usage.tool_calls == 1
    assert [event_type for event_type, _ in sink.events] == [
        EventType.TOOL_PROPOSED,
        EventType.BUDGET_UPDATED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_invalid_arguments_fail_before_budget_reservation(tmp_path: Path) -> None:
    sink = RecordingEventSink()
    context = _context(tmp_path, events=sink)

    with pytest.raises(ToolArgumentsError):
        await ToolExecutor(_registry_with()).execute(
            ToolCall(tool_name="test.echo", arguments={"unknown": "value"}),
            context,
        )

    assert context.budget.usage.tool_calls == 0
    assert [event_type for event_type, _ in sink.events] == [
        EventType.TOOL_PROPOSED,
        EventType.TOOL_FAILED,
    ]


@pytest.mark.asyncio
async def test_missing_capability_is_denied_before_execution(tmp_path: Path) -> None:
    context = _context(tmp_path, lease=CapabilityLease())

    with pytest.raises(CapabilityDeniedError):
        await ToolExecutor(_registry_with()).execute(
            ToolCall(tool_name="test.echo", arguments={"text": "hello"}),
            context,
        )

    assert context.budget.usage.tool_calls == 0


@pytest.mark.asyncio
async def test_timeout_is_classified_and_budget_remains_consumed(tmp_path: Path) -> None:
    async def slow(params: EchoParams, context: ToolExecutionContext) -> str:
        del params, context
        await asyncio.sleep(0.05)
        return "late"

    context = _context(tmp_path)
    registry = _registry_with(slow, policy=_policy(timeout_seconds=0.001))

    with pytest.raises(ToolTimeoutError):
        await ToolExecutor(registry).execute(
            ToolCall(tool_name="test.echo", arguments={"text": "hello"}),
            context,
        )

    assert context.budget.usage.tool_calls == 1


@pytest.mark.asyncio
async def test_unexpected_failure_is_sanitized_and_not_retried(tmp_path: Path) -> None:
    calls = 0

    async def broken(params: EchoParams, context: ToolExecutionContext) -> str:
        nonlocal calls
        del params, context
        calls += 1
        raise RuntimeError("secret implementation detail")

    registry = _registry_with(broken, policy=_policy(idempotent=False))
    context = _context(tmp_path)

    with pytest.raises(ToolExecutionError) as captured:
        await ToolExecutor(registry).execute(
            ToolCall(tool_name="test.echo", arguments={"text": "hello"}),
            context,
        )

    assert calls == 1
    assert "secret implementation detail" not in str(captured.value)


@pytest.mark.asyncio
async def test_oversized_output_requires_artifact_store(tmp_path: Path) -> None:
    async def large(params: EchoParams, context: ToolExecutionContext) -> str:
        del params, context
        return "x" * 100

    registry = _registry_with(large, policy=_policy(max_output_bytes=16))

    with pytest.raises(ToolOutputTooLargeError):
        await ToolExecutor(registry).execute(
            ToolCall(tool_name="test.echo", arguments={"text": "hello"}),
            _context(tmp_path),
        )

    artifacts = RecordingArtifactStore()
    result = await ToolExecutor(registry).execute(
        ToolCall(tool_name="test.echo", arguments={"text": "hello"}),
        _context(tmp_path, artifacts=artifacts),
    )

    assert result.truncated
    assert result.output is None
    assert result.artifact is not None
    assert artifacts.saved


@pytest.mark.asyncio
async def test_proposed_event_redacts_sensitive_arguments_without_mutating_call(
    tmp_path: Path,
) -> None:
    sink = RecordingEventSink()
    context = _context(tmp_path, events=sink)
    call = ToolCall(
        tool_name="test.echo",
        arguments={"text": "hello", "password": "do-not-store"},
    )

    await ToolExecutor(_registry_with()).execute(call, context)

    proposed = sink.events[0][1]
    assert isinstance(proposed, GenericEventPayload)
    assert proposed.data["arguments"] == {"text": "hello", "password": "[REDACTED]"}
    assert call.arguments["password"] == "do-not-store"
