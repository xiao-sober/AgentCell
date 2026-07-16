"""Agent, tool, provider, and memory resource routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from agentcell.agents import AgentSpec
from agentcell.api.dependencies import ApplicationDependency
from agentcell.api.schemas import (
    AgentWriteRequest,
    MemorySearchResponse,
    ProviderResponse,
    ToolResponse,
)
from agentcell.application import AgentCellApplication
from agentcell.errors import AgentRegistrationError, MemoryNotFoundError
from agentcell.memory import MemoryScope

agent_router = APIRouter(prefix="/agents", tags=["agents"])
provider_router = APIRouter(prefix="/providers", tags=["providers"])
tool_router = APIRouter(prefix="/tools", tags=["tools"])
memory_router = APIRouter(prefix="/memories", tags=["memories"])


@agent_router.get("", response_model=list[AgentSpec])
async def list_agents(
    application: ApplicationDependency,
    include_internal: bool = False,
) -> list[AgentSpec]:
    return [
        entry.spec for entry in application.agents.list_entries(include_internal=include_internal)
    ]


@agent_router.post("", response_model=AgentSpec, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentWriteRequest,
    application: ApplicationDependency,
) -> AgentSpec:
    _validate_agent(body.spec, application)
    return await application.create_agent(body.spec)


@agent_router.put("/{agent_id}", response_model=AgentSpec)
async def update_agent(
    agent_id: str,
    body: AgentWriteRequest,
    application: ApplicationDependency,
) -> AgentSpec:
    if body.spec.id != agent_id:
        raise AgentRegistrationError("Agent path ID must match body spec ID")
    _validate_agent(body.spec, application)
    return await application.update_agent(body.spec)


@provider_router.get("", response_model=list[ProviderResponse])
async def list_providers(application: ApplicationDependency) -> list[ProviderResponse]:
    return [
        ProviderResponse(
            model_ref=model_ref,
            provider=spec.provider.value,
            model=spec.model,
            max_output_tokens=spec.max_output_tokens,
            temperature=spec.temperature,
            timeout_seconds=spec.timeout_seconds,
            max_retries=spec.max_retries,
            thinking=getattr(spec, "thinking", None),
        )
        for model_ref, spec in sorted(application.model_specs.items())
    ]


@tool_router.get("", response_model=list[ToolResponse])
async def list_tools(application: ApplicationDependency) -> list[ToolResponse]:
    return [
        ToolResponse(
            name=definition.name,
            description=definition.description,
            parameters=definition.params_model.model_json_schema(),
            risk=definition.policy.risk,
            requires_approval=definition.policy.requires_approval,
            idempotent=definition.policy.idempotent,
            timeout_seconds=definition.policy.timeout_seconds,
            max_output_bytes=definition.policy.max_output_bytes,
            capabilities=sorted(capability.value for capability in definition.policy.capabilities),
        )
        for definition in application.tools.list()
    ]


@memory_router.get("", response_model=list[MemorySearchResponse])
async def search_memories(
    application: ApplicationDependency,
    query: Annotated[str, Query(alias="q", min_length=1)],
    user_id: UUID,
    project_id: Annotated[str, Query(min_length=1, max_length=512)],
    agent_id: str | None = None,
    tags: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[MemorySearchResponse]:
    results = await application.memory.search(
        query,
        scope=MemoryScope(user_id=user_id, project_id=project_id, agent_id=agent_id),
        tags=frozenset(tags or ()),
        limit=limit,
    )
    return [MemorySearchResponse.model_validate(item.model_dump()) for item in results]


@memory_router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: UUID,
    application: ApplicationDependency,
    user_id: UUID,
    project_id: Annotated[str, Query(min_length=1, max_length=512)],
    agent_id: str | None = None,
) -> Response:
    deleted = await application.memory.forget(
        memory_id,
        scope=MemoryScope(user_id=user_id, project_id=project_id, agent_id=agent_id),
    )
    if not deleted:
        raise MemoryNotFoundError("Memory was not found in this scope")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _validate_agent(spec: AgentSpec, application: AgentCellApplication) -> None:
    application.providers.model_spec(spec.model_ref)
    for tool_name in spec.tools:
        application.tools.get(tool_name)
