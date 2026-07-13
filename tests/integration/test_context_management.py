"""Pair-safe trimming, Artifact compaction, summarization, and memory injection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agentcell.agents import AgentRegistry, AgentSpec
from agentcell.events import ArtifactReference, EventType
from agentcell.kernel.run_service import RunRequest, RunService
from agentcell.memory.compaction import PairSafeTrimmer, ToolOutputCompactor
from agentcell.memory.injector import MemoryInjector
from agentcell.memory.models import MemoryCandidate, MemoryKind, MemoryScope
from agentcell.memory.service import MemoryService
from agentcell.memory.summarizer import EpisodicSummarizer
from agentcell.providers import (
    FakeModelSpec,
    FakeProviderAdapter,
    FakeScript,
    FakeTextStep,
    ProviderFactory,
)
from agentcell.storage import Database, EventStore, FileArtifactStore
from agentcell.tools import ToolRegistry


def _paired_history() -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart("keep task")]),
        ModelResponse(parts=[TextPart("old answer")]),
        ModelRequest(parts=[UserPromptPart("use tool")]),
        ModelResponse(parts=[ToolCallPart("workspace.read", {"path": "README.md"}, "call-1")]),
        ModelRequest(parts=[ToolReturnPart("workspace.read", {"content": "value"}, "call-1")]),
        ModelResponse(parts=[TextPart("latest answer")]),
    ]


def test_pair_safe_trimmer_never_orphans_tool_call_or_result() -> None:
    history = _paired_history()
    trimmed = PairSafeTrimmer(2).trim(history)
    call_ids = {
        part.tool_call_id
        for message in trimmed
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }
    result_ids = {
        part.tool_call_id
        for message in trimmed
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }

    assert call_ids == result_ids == {"call-1"}
    assert trimmed[0] == history[0]


@pytest.mark.asyncio
async def test_large_tool_output_is_replaced_by_loadable_artifact(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts = FileArtifactStore(database, tmp_path / "artifacts")
    messages = [
        ModelResponse(parts=[ToolCallPart("test.large", {}, "large-1")]),
        ModelRequest(parts=[ToolReturnPart("test.large", {"content": "x" * 2_000}, "large-1")]),
    ]
    compacted = await ToolOutputCompactor(artifacts, max_inline_bytes=100).compact(messages)
    request = compacted[1]
    assert isinstance(request, ModelRequest)
    part = request.parts[0]
    assert isinstance(part, ToolReturnPart)
    structured = part.structured_content()
    assert isinstance(structured, dict)
    content = cast(dict[str, object], structured)
    reference = ArtifactReference.model_validate(content["artifact"])
    restored = json.loads((await artifacts.load(reference)).decode())

    assert restored == {"content": "x" * 2_000}
    assert PairSafeTrimmer(1, preserve_first=False).trim(compacted) == compacted


@pytest.mark.asyncio
async def test_memory_injector_is_scoped_and_bounded(database: Database) -> None:
    scope = MemoryScope(user_id=uuid4(), project_id="agentcell", agent_id="coder")
    memory = MemoryService(database)
    await memory.remember(
        MemoryCandidate(
            kind=MemoryKind.EPISODIC,
            scope=scope,
            content="Always run Pyright after changing runtime code",
            importance=0.9,
        )
    )
    original: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("change runtime code")])]
    injected = await MemoryInjector(memory, max_items=2, max_characters=200).inject(
        original,
        query="Pyright runtime",
        scope=scope,
    )

    assert len(injected) == 2
    first_part = injected[0].parts[0]
    assert isinstance(first_part, SystemPromptPart)
    assert "Relevant scoped memory" in first_part.content
    assert injected[1:] == original


@pytest.mark.asyncio
async def test_episodic_summarizer_uses_dedicated_fake_model() -> None:
    spec = FakeModelSpec(model="summary-fake")
    providers = ProviderFactory(
        {"summary": spec},
        adapters=(
            FakeProviderAdapter(
                {spec.model: FakeScript(steps=(FakeTextStep(text="Stable episode summary"),))}
            ),
        ),
    )
    try:
        candidate = await EpisodicSummarizer(providers, model_ref="summary").summarize(
            _paired_history(),
            scope=MemoryScope(user_id=uuid4(), project_id="agentcell"),
        )
    finally:
        await providers.aclose()

    assert candidate.kind is MemoryKind.EPISODIC
    assert candidate.content == "Stable episode summary"


@pytest.mark.asyncio
async def test_run_service_injects_scoped_memory_and_records_recall(
    database: Database,
    tmp_path: Path,
) -> None:
    user_id = uuid4()
    workspace = await asyncio.to_thread(tmp_path.resolve)
    scope = MemoryScope(
        user_id=user_id,
        project_id=str(workspace),
        agent_id="coordinator",
    )
    await MemoryService(database).remember(
        MemoryCandidate(
            kind=MemoryKind.EPISODIC,
            scope=scope,
            content="Runtime changes require Pyright",
        )
    )
    spec = FakeModelSpec(model="memory-run")
    providers = ProviderFactory(
        {"fake_memory": spec},
        adapters=(
            FakeProviderAdapter(
                {spec.model: FakeScript(steps=(FakeTextStep(text="memory used"),))}
            ),
        ),
    )
    agent = AgentSpec(
        id="coordinator",
        name="Coordinator",
        description="Memory injection test.",
        model_ref="fake_memory",
        instructions="Use relevant memory.",
    )
    service = RunService(
        database=database,
        providers=providers,
        agents=AgentRegistry((agent,)),
        tools=ToolRegistry(),
        artifact_root=tmp_path / "artifacts",
    )
    try:
        result = await service.run(
            RunRequest(
                prompt="change Runtime and run Pyright",
                workspace=workspace,
                user_id=user_id,
            )
        )
    finally:
        await providers.aclose()

    async with database.session() as session:
        events = await EventStore(session).list_for_run(result.run.id)
    assert EventType.MEMORY_RECALLED in [event.event_type for event in events]
