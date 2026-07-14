"""Transport-neutral Conversation creation, history loading, and Run orchestration."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.budgets import Budget
from agentcell.conversations.models import (
    Conversation,
    ConversationMessage,
    ConversationMessageKind,
)
from agentcell.errors import ConversationScopeError
from agentcell.events import JsonValue
from agentcell.kernel.models import Run
from agentcell.kernel.run_service import RunRequest, RunResult, RunService
from agentcell.memory.compaction import PairSafeTokenTrimmer, PairSafeTrimmer, ToolOutputCompactor
from agentcell.policy import CapabilityLease, PermissionMode
from agentcell.storage import (
    ConversationMessageRepository,
    ConversationRepository,
    Database,
    FileArtifactStore,
)


@dataclass(frozen=True, slots=True)
class PreparedConversationTurn:
    run: Run
    request: RunRequest
    spec: AgentSpec
    history: tuple[ModelMessage, ...]


class ConversationService:
    """Keep Conversation scope stable while every turn receives a fresh Run budget."""

    def __init__(
        self,
        *,
        database: Database,
        runs: RunService,
        agents: AgentRegistry,
        artifact_root: Path = Path(".agentcell/artifacts"),
    ) -> None:
        self._database = database
        self._runs = runs
        self._agents = agents
        self._artifacts = FileArtifactStore(database, artifact_root)
        self._tool_compactor = ToolOutputCompactor(self._artifacts)

    async def create(
        self,
        *,
        user_id: UUID,
        workspace: Path,
        agent_id: str = "coordinator",
        project_id: str | None = None,
        title: str | None = None,
        conversation_id: UUID | None = None,
    ) -> Conversation:
        self._agents.get(agent_id)
        resolved = await asyncio.to_thread(workspace.resolve, strict=True)
        if not await asyncio.to_thread(resolved.is_dir):
            raise ValueError("workspace must be a directory")
        conversation = Conversation(
            id=conversation_id or uuid4(),
            user_id=user_id,
            project_id=project_id or str(resolved),
            workspace=str(resolved),
            agent_id=agent_id,
            title=title,
        )
        async with self._database.transaction() as session:
            await ConversationRepository(session).create(conversation)
        return conversation

    async def get(self, conversation_id: UUID, *, user_id: UUID | None = None) -> Conversation:
        async with self._database.session() as session:
            conversation = await ConversationRepository(session).get_required(conversation_id)
        self._ensure_user(conversation, user_id)
        return conversation

    async def list(self, user_id: UUID, *, limit: int = 100) -> tuple[Conversation, ...]:
        async with self._database.session() as session:
            items = await ConversationRepository(session).list_for_user(user_id, limit=limit)
        return tuple(items)

    async def messages(
        self,
        conversation_id: UUID,
        *,
        user_id: UUID | None = None,
        limit: int = 500,
    ) -> tuple[ConversationMessage, ...]:
        conversation = await self.get(conversation_id, user_id=user_id)
        async with self._database.session() as session:
            items = await ConversationMessageRepository(session).list_for_conversation(
                conversation.id, limit=limit
            )
        return tuple(items)

    async def prepare_turn(
        self,
        conversation_id: UUID,
        *,
        prompt: str,
        user_id: UUID | None = None,
        lease: CapabilityLease | None = None,
        permission_mode: PermissionMode = PermissionMode.REQUEST,
        budget: Budget | None = None,
        run_id: UUID | None = None,
    ) -> PreparedConversationTurn:
        selected_run_id = run_id or uuid4()
        async with self._database.transaction() as session:
            repository = ConversationRepository(session)
            conversation = await repository.get_required(conversation_id)
            self._ensure_user(conversation, user_id)
            await repository.claim(conversation_id, selected_run_id)
            stored = await ConversationMessageRepository(session).list_for_conversation(
                conversation_id,
                completed_only=True,
            )
        history = self._restore_history(stored)
        request = RunRequest(
            prompt=prompt,
            workspace=Path(conversation.workspace),
            agent_id=conversation.agent_id,
            conversation_id=conversation.id,
            user_id=conversation.user_id,
            lease=lease
            or CapabilityLease(filesystem_read=(".",), can_delegate=True, max_child_depth=2),
            permission_mode=permission_mode,
            run_id=selected_run_id,
        )
        if budget is not None:
            request = request.model_copy(update={"budget": budget})
        try:
            run, spec = await self._runs.prepare(request)
            user_message = ModelRequest(parts=[UserPromptPart(prompt)])
            payload = self._dump_message(user_message)
            async with self._database.transaction() as session:
                await ConversationMessageRepository(session).append(
                    conversation_id=conversation.id,
                    run_id=run.id,
                    kind=ConversationMessageKind.REQUEST,
                    payload=payload,
                )
        except BaseException:
            async with self._database.transaction() as session:
                await ConversationRepository(session).release(conversation.id, selected_run_id)
            raise
        return PreparedConversationTurn(run, request, spec, tuple(history))

    async def execute_prepared(self, prepared: PreparedConversationTurn) -> RunResult:
        try:
            result = await self._runs.execute_prepared(
                prepared.run,
                request=prepared.request,
                spec=prepared.spec,
                message_history=prepared.history,
            )
        except BaseException:
            await self._release(prepared.run)
            raise
        await self.record_result(result)
        return result

    async def run_turn(self, conversation_id: UUID, **values: object) -> RunResult:
        prepared = await self.prepare_turn(conversation_id, **values)  # type: ignore[arg-type]
        return await self.execute_prepared(prepared)

    async def record_result(self, result: RunResult) -> None:
        messages = ModelMessagesTypeAdapter.validate_json(result.messages_json)
        sanitized = await self._sanitize(messages)
        async with self._database.transaction() as session:
            repository = ConversationMessageRepository(session)
            existing = await repository.list_for_conversation(result.run.conversation_id)
            current_payloads = Counter(
                self._payload_key(item.payload) for item in existing if item.run_id == result.run.id
            )
            seen: Counter[str] = Counter()
            for message in sanitized:
                payload = self._dump_message(message)
                key = self._payload_key(payload)
                seen[key] += 1
                if seen[key] <= current_payloads[key]:
                    continue
                await repository.append(
                    conversation_id=result.run.conversation_id,
                    run_id=result.run.id,
                    kind=(
                        ConversationMessageKind.REQUEST
                        if isinstance(message, ModelRequest)
                        else ConversationMessageKind.RESPONSE
                    ),
                    payload=payload,
                    artifact_ids=self._artifact_ids(payload),
                )
            if result.run.status.is_terminal:
                await ConversationRepository(session).release(
                    result.run.conversation_id, result.run.id
                )

    async def record_if_managed(self, result: RunResult) -> None:
        """Project a resumed managed Run while leaving standalone Runs untouched."""

        async with self._database.session() as session:
            conversation = await ConversationRepository(session).get(result.run.conversation_id)
        if conversation is not None:
            await self.record_result(result)

    async def release_if_managed(self, run: Run) -> None:
        async with self._database.session() as session:
            conversation = await ConversationRepository(session).get(run.conversation_id)
        if conversation is not None:
            await self._release(run)

    async def _release(self, run: Run) -> None:
        async with self._database.transaction() as session:
            await ConversationRepository(session).release(run.conversation_id, run.id)

    @staticmethod
    def _ensure_user(conversation: Conversation, user_id: UUID | None) -> None:
        if user_id is not None and conversation.user_id != user_id:
            raise ConversationScopeError("Conversation user scope does not match")

    @staticmethod
    def _restore_history(messages: Sequence[ConversationMessage]) -> list[ModelMessage]:
        restored: list[ModelMessage] = []
        for item in messages:
            restored.extend(ModelMessagesTypeAdapter.validate_python([item.payload]))
        restored = PairSafeTrimmer(100).trim(restored)
        return PairSafeTokenTrimmer(32_000).trim(restored)

    async def _sanitize(self, messages: Sequence[ModelMessage]) -> list[ModelMessage]:
        safe: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelRequest):
                parts: list[ModelRequestPart] = []
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        parts.append(UserPromptPart(part.content))
                    elif isinstance(part, ToolReturnPart):
                        parts.append(
                            ToolReturnPart(
                                part.tool_name,
                                part.content,
                                part.tool_call_id,
                                outcome=part.outcome,
                            )
                        )
                if parts:
                    safe.append(ModelRequest(parts=parts))
            else:
                response_parts: list[ModelResponsePart] = []
                for part in message.parts:
                    if isinstance(part, TextPart):
                        response_parts.append(TextPart(part.content))
                    elif isinstance(part, ToolCallPart):
                        response_parts.append(
                            ToolCallPart(
                                part.tool_name,
                                part.args_as_dict(),
                                part.tool_call_id,
                            )
                        )
                if response_parts:
                    safe.append(ModelResponse(parts=response_parts))
        return await self._tool_compactor.compact(safe)

    @staticmethod
    def _dump_message(message: ModelMessage) -> dict[str, JsonValue]:
        value = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
        if not isinstance(value, dict):
            raise ValueError("Serialized model message must be an object")
        return cast(dict[str, JsonValue], value)

    @staticmethod
    def _payload_key(payload: dict[str, JsonValue]) -> str:
        normalized = dict(payload)
        normalized.pop("timestamp", None)
        parts = normalized.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict):
                    part.pop("timestamp", None)
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _artifact_ids(cls, payload: JsonValue) -> tuple[UUID, ...]:
        found: list[UUID] = []

        def visit(value: JsonValue) -> None:
            if isinstance(value, dict):
                artifact_id = value.get("artifact_id")
                if isinstance(artifact_id, str):
                    try:
                        found.append(UUID(artifact_id))
                    except ValueError:
                        pass
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(payload)
        return tuple(dict.fromkeys(found))
