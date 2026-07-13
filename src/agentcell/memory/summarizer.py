"""Bounded low-temperature episodic summarization through ProviderFactory."""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent, ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage
from pydantic_ai.settings import ModelSettings

from agentcell.errors import ConfigurationError
from agentcell.memory.models import MemoryCandidate, MemoryKind, MemoryScope
from agentcell.providers import ProviderFactory


class EpisodicSummarizer:
    """Generate one bounded episode candidate using a dedicated cheap model ref."""

    def __init__(
        self,
        providers: ProviderFactory,
        *,
        model_ref: str,
        timeout_seconds: float = 30,
        max_input_characters: int = 32_000,
        max_output_tokens: int = 1_024,
    ) -> None:
        spec = providers.model_spec(model_ref)
        if bool(getattr(spec, "thinking", False)):
            raise ConfigurationError("Summary model must disable deep thinking")
        self._providers = providers
        self._model_ref = model_ref
        self._timeout_seconds = timeout_seconds
        self._max_input_characters = max_input_characters
        self._max_output_tokens = max_output_tokens

    async def summarize(
        self,
        messages: list[ModelMessage],
        *,
        scope: MemoryScope,
        tags: frozenset[str] = frozenset({"episode"}),
    ) -> MemoryCandidate:
        serialized = ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
        bounded = serialized[-self._max_input_characters :]
        model = await self._providers.build_model(self._model_ref)
        agent = Agent(
            model,
            instructions=(
                "Summarize durable task facts, decisions, results, and unresolved risks. "
                "Do not include credentials or hidden reasoning."
            ),
        )
        settings = ModelSettings(temperature=0, max_tokens=self._max_output_tokens)
        async with asyncio.timeout(self._timeout_seconds):
            result = await agent.run(bounded, model_settings=settings)
        return MemoryCandidate(
            kind=MemoryKind.EPISODIC,
            scope=scope,
            content=result.output,
            tags=tags,
            importance=0.7,
        )
