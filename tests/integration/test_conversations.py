"""Stage 9.1 durable Conversation threads and bounded history inheritance."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ThinkingPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from agentcell.agents import AgentRegistry, coordinator_spec
from agentcell.conversations.service import ConversationService
from agentcell.errors import ConversationConflictError, ConversationScopeError
from agentcell.kernel.run_service import RunService
from agentcell.providers import FakeModelSpec, ModelSpec, ProviderFactory, ProviderName
from agentcell.storage import Database
from agentcell.tools import ToolRegistry, register_workspace_tools


class RecordingAdapter:
    provider = ProviderName.FAKE
    requires_api_key = False
    uses_http_client = False
    cacheable = False

    def __init__(self) -> None:
        self.seen: list[str] = []

    def build_model(
        self,
        spec: ModelSpec[ProviderName],
        *,
        api_key: str | None,
        http_client: httpx.AsyncClient | None,
    ) -> Model:
        del api_key, http_client

        def record(messages: list[ModelMessage]) -> int:
            self.seen.append(ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8"))
            return len(self.seen)

        async def request(
            messages: list[ModelMessage],
            agent_info: AgentInfo,
        ) -> ModelResponse:
            del agent_info
            turn = record(messages)
            return ModelResponse(
                parts=[ThinkingPart("private reasoning"), TextPart(f"reply-{turn}")]
            )

        async def stream(
            messages: list[ModelMessage],
            agent_info: AgentInfo,
        ) -> AsyncIterator[str]:
            del agent_info
            turn = record(messages)
            yield f"reply-{turn}"

        return FunctionModel(request, stream_function=stream, model_name=spec.model)


def make_service(
    database: Database,
    tmp_path: Path,
    adapter: RecordingAdapter,
) -> tuple[ConversationService, ProviderFactory]:
    model = FakeModelSpec(model="conversation-history-fake")
    providers = ProviderFactory(
        {"conversation_fake": model},
        adapters=(adapter,),
    )
    agents = AgentRegistry((coordinator_spec(model_ref="conversation_fake"),))
    tools = ToolRegistry()
    register_workspace_tools(tools)
    runs = RunService(
        database=database,
        providers=providers,
        agents=agents,
        tools=tools,
        artifact_root=tmp_path / "artifacts",
    )
    return (
        ConversationService(
            database=database,
            runs=runs,
            agents=agents,
            artifact_root=tmp_path / "artifacts",
        ),
        providers,
    )


@pytest.mark.asyncio
async def test_conversation_inherits_completed_history_across_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    first_adapter = RecordingAdapter()
    service, providers = make_service(database, tmp_path, first_adapter)
    conversation = await service.create(user_id=user_id, workspace=tmp_path)
    first = await service.run_turn(
        conversation.id,
        prompt="first question",
        user_id=user_id,
    )
    assert first.output == "reply-1"
    await providers.aclose()

    restarted_adapter = RecordingAdapter()
    restarted, restarted_providers = make_service(database, tmp_path, restarted_adapter)
    second = await restarted.run_turn(
        conversation.id,
        prompt="follow-up question",
        user_id=user_id,
    )
    assert second.run.id != first.run.id
    assert "first question" in restarted_adapter.seen[0]
    assert "reply-1" in restarted_adapter.seen[0]
    messages = await restarted.messages(conversation.id, user_id=user_id)
    assert [item.sequence for item in messages] == list(range(1, len(messages) + 1))
    assert {item.run_id for item in messages} == {first.run.id, second.run.id}
    assert "private reasoning" not in str([item.payload for item in messages])
    await restarted_providers.aclose()


@pytest.mark.asyncio
async def test_conversation_rejects_scope_mismatch_and_parallel_root_turns(
    database: Database,
    tmp_path: Path,
) -> None:
    adapter = RecordingAdapter()
    service, providers = make_service(database, tmp_path, adapter)
    user_id = uuid4()
    conversation = await service.create(user_id=user_id, workspace=tmp_path)

    with pytest.raises(ConversationScopeError):
        await service.get(conversation.id, user_id=uuid4())

    prepared = await service.prepare_turn(
        conversation.id,
        prompt="held turn",
        user_id=user_id,
    )
    with pytest.raises(ConversationConflictError):
        await service.prepare_turn(
            conversation.id,
            prompt="parallel turn",
            user_id=user_id,
        )
    result = await service.execute_prepared(prepared)
    assert result.run.status.is_terminal
    await providers.aclose()
