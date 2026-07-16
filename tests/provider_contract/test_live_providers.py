"""Explicitly gated end-to-end smoke tests for paid Provider endpoints."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_ai import models

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.config import AgentCellSettings
from agentcell.events import EventType
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.policy import Capability, CapabilityLease, RiskLevel, ToolPolicy
from agentcell.providers import ProviderFactory
from agentcell.storage import Database, EventStore
from agentcell.tools import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
)

_LIVE_FLAG = "AGENTCELL_RUN_LIVE_PROVIDER_TESTS"


def _live_enabled() -> bool:
    return os.getenv(_LIVE_FLAG, "").casefold() in {"1", "true", "yes"}


def _require_live(key_env: str) -> None:
    if not _live_enabled():
        pytest.skip(f"set {_LIVE_FLAG}=1 to allow paid Provider tests")
    if not os.getenv(key_env):
        pytest.skip(f"{key_env} is not configured")


def _runtime(
    database: Database,
    providers: ProviderFactory,
    model_ref: str,
    *,
    registry: ToolRegistry | None = None,
    tool_names: tuple[str, ...] = (),
) -> RunService:
    capabilities: frozenset[Capability] = (
        frozenset({Capability.FILESYSTEM_READ}) if tool_names else frozenset()
    )
    agent = AgentSpec(
        id="live_contract",
        name="Live Contract",
        description="Paid end-to-end Provider contract Agent.",
        model_ref=model_ref,
        instructions="Follow the contract prompt exactly and keep the answer brief.",
        tools=tool_names,
        capabilities=capabilities,
        max_steps=8,
    )
    return RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry((agent,)),
        tools=registry or ToolRegistry(),
    )


async def _events(database: Database, run_id: UUID):
    async with database.session() as session:
        return await EventStore(session).list_for_run(run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_ref", "key_env"),
    [
        ("qwen_plus", "DASHSCOPE_API_KEY"),
        ("deepseek_pro", "DEEPSEEK_API_KEY"),
    ],
)
async def test_live_provider_text_usage_events_budget_and_terminal(
    model_ref: str,
    key_env: str,
    database: Database,
    tmp_path: Path,
) -> None:
    _require_live(key_env)
    settings = AgentCellSettings.from_toml("agentcell.toml")
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True
    providers = ProviderFactory(settings.models)
    try:
        result = await _runtime(database, providers, model_ref).run(
            RunRequest(
                prompt="Reply with exactly: AgentCell provider contract OK",
                workspace=tmp_path,
                agent_id="live_contract",
            )
        )
    finally:
        await providers.aclose()
        models.ALLOW_MODEL_REQUESTS = previous

    events = await _events(database, result.run.id)
    assert result.output
    assert result.run.status.value == "completed"
    assert result.budget.used.requests >= 1
    assert result.budget.used.output_tokens > 0
    assert EventType.MODEL_REQUESTED in {event.event_type for event in events}
    assert EventType.MODEL_COMPLETED in {event.event_type for event in events}
    assert events[-1].event_type is EventType.RUN_COMPLETED
    if model_ref == "deepseek_pro":
        assert result.budget.used.cache_read_tokens >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_ref", "key_env"),
    [
        ("qwen_plus", "DASHSCOPE_API_KEY"),
        ("deepseek_pro", "DEEPSEEK_API_KEY"),
    ],
)
async def test_live_provider_streaming_is_persisted_as_public_deltas(
    model_ref: str,
    key_env: str,
    database: Database,
    tmp_path: Path,
) -> None:
    _require_live(key_env)
    settings = AgentCellSettings.from_toml("agentcell.toml")
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True
    providers = ProviderFactory(settings.models)
    try:
        result = await _runtime(database, providers, model_ref).run(
            RunRequest(
                prompt="Reply with exactly: AgentCell streaming contract OK",
                workspace=tmp_path,
                agent_id="live_contract",
            )
        )
    finally:
        await providers.aclose()
        models.ALLOW_MODEL_REQUESTS = previous

    events = await _events(database, result.run.id)
    assert result.run.status.value == "completed"
    assert result.budget.used.requests >= 1
    assert EventType.MODEL_TEXT_DELTA in {event.event_type for event in events}
    assert events[-1].event_type is EventType.RUN_COMPLETED


class ContractValueParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


async def _contract_value(
    params: ContractValueParams,
    context: ToolExecutionContext,
) -> str:
    del params, context
    return "AGENTCELL_TOOL_OK"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_ref", "key_env"),
    [
        ("qwen_plus", "DASHSCOPE_API_KEY"),
        ("deepseek_pro", "DEEPSEEK_API_KEY"),
    ],
)
async def test_live_provider_function_calling_uses_runtime_tool_boundary(
    model_ref: str,
    key_env: str,
    database: Database,
    tmp_path: Path,
) -> None:
    _require_live(key_env)
    settings = AgentCellSettings.from_toml("agentcell.toml")
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="contract.value",
            description="Return the exact value required by this Provider contract test.",
            params_model=ContractValueParams,
            policy=ToolPolicy(
                risk=RiskLevel.SAFE,
                requires_approval=False,
                idempotent=True,
                timeout_seconds=5,
                max_output_bytes=1_024,
                capabilities=frozenset({Capability.FILESYSTEM_READ}),
            ),
            handler=_contract_value,
        )
    )
    providers = ProviderFactory(settings.models)
    try:
        result = await _runtime(
            database,
            providers,
            model_ref,
            registry=registry,
            tool_names=("contract.value",),
        ).run(
            RunRequest(
                prompt=(
                    "Call contract_value exactly once, then reply with only its returned value."
                ),
                workspace=tmp_path,
                agent_id="live_contract",
                lease=CapabilityLease(filesystem_read=(".",)),
            )
        )
    finally:
        await providers.aclose()
        models.ALLOW_MODEL_REQUESTS = previous

    events = await _events(database, result.run.id)
    assert result.output is not None
    assert result.output.strip() == "AGENTCELL_TOOL_OK"
    assert result.run.status.value == "completed"
    assert result.budget.used.tool_calls == 1
    assert EventType.TOOL_COMPLETED in {event.event_type for event in events}
    assert events[-1].event_type is EventType.RUN_COMPLETED
