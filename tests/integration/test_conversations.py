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
from agentcell.application import build_application
from agentcell.conversations import ConversationRoutingMode
from agentcell.conversations.service import ConversationService
from agentcell.errors import (
    ConversationConflictError,
    ConversationModelBindingError,
    ConversationScopeError,
)
from agentcell.events import EventType
from agentcell.kernel.run_service import RunService
from agentcell.providers import FakeModelSpec, ModelSpec, ProviderFactory, ProviderName
from agentcell.storage import Database, EventStore
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
            model_refs={"conversation_fake"},
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


@pytest.mark.asyncio
async def test_conversation_keeps_bound_model_across_application_restart(
    database: Database,
    tmp_path: Path,
) -> None:
    models = {
        "qwen_fake": FakeModelSpec(model="qwen-fake"),
        "deepseek_fake": FakeModelSpec(model="deepseek-fake"),
    }

    def build(adapter: RecordingAdapter) -> tuple[ConversationService, ProviderFactory]:
        providers = ProviderFactory(models, adapters=(adapter,))
        agents = AgentRegistry((coordinator_spec(model_ref="qwen_fake"),))
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
                model_refs=models,
                artifact_root=tmp_path / "artifacts",
            ),
            providers,
        )

    user_id = uuid4()
    first_service, first_providers = build(RecordingAdapter())
    conversation = await first_service.create(
        user_id=user_id,
        workspace=tmp_path,
        model_ref="deepseek_fake",
    )
    first = await first_service.run_turn(
        conversation.id,
        prompt="use deepseek",
        user_id=user_id,
    )
    await first_providers.aclose()

    restarted, restarted_providers = build(RecordingAdapter())
    restored = await restarted.get(conversation.id, user_id=user_id)
    second = await restarted.run_turn(
        conversation.id,
        prompt="keep the same model",
        user_id=user_id,
    )

    assert restored.model_ref == "deepseek_fake"
    assert first.run.execution_identity is not None
    assert second.run.execution_identity is not None
    assert first.run.execution_identity.model_ref == "deepseek_fake"
    assert second.run.execution_identity.model_ref == "deepseek_fake"
    with pytest.raises(ConversationModelBindingError):
        await restarted.prepare_turn(
            conversation.id,
            prompt="attempt model drift",
            user_id=user_id,
            model_ref="qwen_fake",
        )
    await restarted_providers.aclose()


@pytest.mark.asyncio
async def test_auto_conversation_runs_ordinary_question_without_router_or_child(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    application = await build_application(
        database_url=migrated_database_url,
        offline_fake=True,
        fake_output="I am AgentCell.",
    )
    user_id = uuid4()
    try:
        conversation = await application.conversations.create(
            user_id=user_id,
            workspace=tmp_path,
            routing_mode=ConversationRoutingMode.AUTO,
        )
        assert application.conversations.should_use_direct_turn(
            conversation,
            prompt="你是谁？",
        )
        prepared = await application.conversations.prepare_direct_turn(
            conversation.id,
            prompt="你是谁？",
            user_id=user_id,
            budget=application.teams.get("software").default_budget,
        )
        result = await application.conversations.execute_prepared(prepared)
        async with application.database.session() as session:
            events = await EventStore(session).list_for_run(result.run.id)
    finally:
        await application.close()

    assert result.output == "I am AgentCell."
    assert result.run.agent_id == "assistant"
    assert result.run.parent_run_id is None
    assert result.budget.budget.max_tool_calls == 0
    assert result.budget.budget.max_children == 0
    assert EventType.TASK_ROUTE_PROPOSED not in {event.event_type for event in events}
    assert EventType.AGENT_CHILD_STARTED not in {event.event_type for event in events}
